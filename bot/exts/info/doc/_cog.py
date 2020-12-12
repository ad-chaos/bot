from __future__ import annotations

import asyncio
import logging
import re
import sys
from collections import defaultdict
from contextlib import suppress
from typing import Dict, List, NamedTuple, Optional, Union

import discord
from bs4 import BeautifulSoup
from discord.ext import commands

from bot import instance as bot_instance
from bot.bot import Bot
from bot.constants import MODERATION_ROLES, RedirectOutput
from bot.converters import InventoryURL, PackageName, ValidURL
from bot.pagination import LinePaginator
from bot.utils.lock import lock
from bot.utils.messages import wait_for_deletion
from bot.utils.scheduling import Scheduler
from ._inventory_parser import fetch_inventory
from ._parsing import get_symbol_markdown
from ._redis_cache import DocRedisCache

log = logging.getLogger(__name__)

# symbols with a group contained here will get the group prefixed on duplicates
FORCE_PREFIX_GROUPS = (
    "2to3fixer",
    "token",
    "label",
    "pdbcommand",
    "term",
)
PRIORITY_PACKAGES = (
    "python",
)
WHITESPACE_AFTER_NEWLINES_RE = re.compile(r"(?<=\n\n)(\s+)")
NOT_FOUND_DELETE_DELAY = RedirectOutput.delete_delay

REFRESH_EVENT = asyncio.Event()
REFRESH_EVENT.set()
COMMAND_LOCK_SINGLETON = "inventory refresh"

doc_cache = DocRedisCache(namespace="Docs")


class DocItem(NamedTuple):
    """Holds inventory symbol information."""

    package: str
    group: str
    base_url: str
    relative_url_path: str
    symbol_id: str

    @property
    def url(self) -> str:
        """Return the absolute url to the symbol."""
        return "".join((self.base_url, self.relative_url_path))


class QueueItem(NamedTuple):
    """Contains a symbol and the BeautifulSoup object needed to parse it."""

    symbol: DocItem
    soup: BeautifulSoup

    def __eq__(self, other: Union[QueueItem, DocItem]):
        if isinstance(other, DocItem):
            return self.symbol == other
        return NamedTuple.__eq__(self, other)


class CachedParser:
    """
    Get symbol markdown from pages with smarter caching.

    DocItems are added through the `add_item` method which adds them to the `_page_symbols` dict.
    `get_markdown` is used to fetch the markdown; when this is used for the first time on a page,
    all of the symbols are queued to be parsed to avoid multiple web requests to the same page.
    """

    def __init__(self):
        self._queue: List[QueueItem] = []
        self._page_symbols: Dict[str, List[DocItem]] = defaultdict(list)
        self._item_futures: Dict[DocItem, asyncio.Future] = {}
        self._parse_task = None

    async def get_markdown(self, doc_item: DocItem) -> str:
        """
        Get result markdown of `doc_item`.

        If no symbols were fetched from `doc_item`s page before,
        the HTML has to be fetched before parsing can be queued.
        """
        if (symbols_to_queue := self._page_symbols.get(doc_item.url)) is not None:
            async with bot_instance.http_session.get(doc_item.url) as response:
                soup = BeautifulSoup(await response.text(encoding="utf8"), "lxml")

            self._queue.extend(QueueItem(symbol, soup) for symbol in symbols_to_queue)
            del self._page_symbols[doc_item.url]
            log.debug(f"Added symbols from {doc_item.url} to parse queue.")

            if self._parse_task is None:
                self._parse_task = asyncio.create_task(self._parse_queue())

        self._move_to_front(doc_item)
        if doc_item not in self._item_futures:
            self._item_futures[doc_item] = bot_instance.loop.create_future()
        return await self._item_futures[doc_item]

    async def _parse_queue(self) -> None:
        """
        Parse all item from the queue, setting associated events for symbols if present.

        The coroutine will run as long as the queue is not empty, resetting `self._parse_task` to None when finished.
        """
        log.trace("Starting queue parsing.")
        try:
            while self._queue:
                item, soup = self._queue.pop()
                try:
                    markdown = get_symbol_markdown(soup, item)
                    await doc_cache.set(item, markdown)
                except Exception:
                    log.exception(f"Unexpected error when handling {item}")
                else:
                    if (future := self._item_futures.get(item)) is not None:
                        future.set_result(markdown)
                await asyncio.sleep(0.1)
        finally:
            self._parse_task = None
            log.trace("Finished parsing queue.")

    def _move_to_front(self, item: Union[QueueItem, DocItem]) -> None:
        """Map a DocItem to its page so that the symbol will be parsed once the page is requested."""
        # The parse queue stores soups along with the doc symbols in QueueItem objects,
        # in case we're moving a DocItem we have to get the associated QueueItem first and then move it.
        item_index = self._queue.index(item)
        queue_item = self._queue.pop(item_index)

        self._queue.append(queue_item)

    def add_item(self, doc_item: DocItem) -> None:
        """Add a DocItem to `_page_symbols`."""
        self._page_symbols[doc_item.url].append(doc_item)

    async def clear(self) -> None:
        """
        Clear all internal symbol data.

        All currently requested items are waited to be parsed before clearing.
        """
        for future in self._item_futures.values():
            await future
        if self._parse_task is not None:
            self._parse_task.cancel()
        self._queue.clear()
        self._page_symbols.clear()
        self._item_futures.clear()


class DocCog(commands.Cog):
    """A set of commands for querying & displaying documentation."""

    def __init__(self, bot: Bot):
        self.base_urls = {}
        self.bot = bot
        self.doc_symbols: Dict[str, DocItem] = {}
        self.item_fetcher = CachedParser()
        self.renamed_symbols = set()

        self.inventory_scheduler = Scheduler(self.__class__.__name__)
        self.scheduled_inventories = set()

        self.bot.loop.create_task(self.init_refresh_inventory())

    @lock("doc", COMMAND_LOCK_SINGLETON, raise_error=True)
    async def init_refresh_inventory(self) -> None:
        """Refresh documentation inventory on cog initialization."""
        await self.bot.wait_until_guild_available()
        await self.refresh_inventory()

    async def update_single(
        self, api_package_name: str, base_url: str, inventory_url: str
    ) -> bool:
        """
        Rebuild the inventory for a single package.

        Where:
            * `package_name` is the package name to use, appears in the log
            * `base_url` is the root documentation URL for the specified package, used to build
                absolute paths that link to specific symbols
            * `inventory_url` is the absolute URL to the intersphinx inventory.

        If the inventory file is currently unreachable,
        the update is rescheduled to execute in 2 minutes on the first attempt, and 5 minutes on subsequent attempts.

        Return True on success; False if fetching failed and was rescheduled.
        """
        self.base_urls[api_package_name] = base_url
        package = await fetch_inventory(inventory_url)

        if not package:
            delay = 2*60 if inventory_url not in self.scheduled_inventories else 5*60
            log.info(f"Failed to fetch inventory; attempting again in {delay//60} minutes.")
            self.inventory_scheduler.schedule_later(
                delay,
                api_package_name,
                fetch_inventory(inventory_url)
            )
            self.scheduled_inventories.add(api_package_name)
            return False

        self.scheduled_inventories.discard(api_package_name)

        for group, items in package.items():
            for symbol, relative_doc_url in items:
                if "/" in symbol:
                    continue  # skip unreachable symbols with slashes

                group_name = group.split(":")[1]
                if (original_symbol := self.doc_symbols.get(symbol)) is not None:
                    if group_name in FORCE_PREFIX_GROUPS:
                        symbol = f"{group_name}.{symbol}"
                        self.renamed_symbols.add(symbol)

                    elif (original_symbol_group := original_symbol.group) in FORCE_PREFIX_GROUPS:
                        overridden_symbol = f"{original_symbol_group}.{symbol}"
                        if overridden_symbol in self.renamed_symbols:
                            overridden_symbol = f"{api_package_name}.{overridden_symbol}"

                        self.doc_symbols[overridden_symbol] = original_symbol
                        self.renamed_symbols.add(overridden_symbol)

                    elif api_package_name in PRIORITY_PACKAGES:
                        self.doc_symbols[f"{original_symbol.package}.{symbol}"] = original_symbol
                        self.renamed_symbols.add(symbol)

                    else:
                        symbol = f"{api_package_name}.{symbol}"
                        self.renamed_symbols.add(symbol)

                relative_url_path, _, symbol_id = relative_doc_url.partition("#")
                # Intern fields that have shared content so we're not storing unique strings for every object
                symbol_item = DocItem(
                    api_package_name,
                    sys.intern(group_name),
                    base_url,
                    sys.intern(relative_url_path),
                    symbol_id
                )
                self.doc_symbols[symbol] = symbol_item
                self.item_fetcher.add_item(symbol_item)

        log.trace(f"Fetched inventory for {api_package_name}.")
        return True

    async def refresh_inventory(self) -> None:
        """Refresh internal documentation inventory."""
        REFRESH_EVENT.clear()
        log.debug("Refreshing documentation inventory...")
        for inventory in self.scheduled_inventories:
            self.inventory_scheduler.cancel(inventory)

        # Clear the old base URLS and doc symbols to ensure
        # that we start from a fresh local dataset.
        # Also, reset the cache used for fetching documentation.
        self.base_urls.clear()
        self.doc_symbols.clear()
        self.renamed_symbols.clear()
        self.scheduled_inventories.clear()
        await self.item_fetcher.clear()

        # Run all coroutines concurrently - since each of them performs an HTTP
        # request, this speeds up fetching the inventory data heavily.
        coros = [
            self.update_single(
                package["package"], package["base_url"], package["inventory_url"]
            ) for package in await self.bot.api_client.get('bot/documentation-links')
        ]
        await asyncio.gather(*coros)
        REFRESH_EVENT.set()

    async def get_symbol_embed(self, symbol: str) -> Optional[discord.Embed]:
        """
        Attempt to scrape and fetch the data for the given `symbol`, and build an embed from its contents.

        If the symbol is known, an Embed with documentation about it is returned.

        First check the DocRedisCache before querying the cog's `CachedParser`,
        if not present also create a redis entry for the symbol.
        """
        log.trace(f"Building embed for symbol `{symbol}`")
        symbol_info = self.doc_symbols.get(symbol)
        if symbol_info is None:
            log.debug("Symbol does not exist.")
            return None
        self.bot.stats.incr(f"doc_fetches.{symbol_info.package.lower()}")

        markdown = await doc_cache.get(symbol_info)
        if markdown is None:
            log.debug(f"Redis cache miss for symbol `{symbol}`.")
            if not REFRESH_EVENT.is_set():
                log.debug("Waiting for inventories to be refreshed before processing item.")
                await REFRESH_EVENT.wait()
            markdown = await self.item_fetcher.get_markdown(symbol_info)
            if markdown is not None:
                await doc_cache.set(symbol_info, markdown)
            else:
                markdown = "Unable to parse the requested symbol."

        embed = discord.Embed(
            title=discord.utils.escape_markdown(symbol),
            url=f"{symbol_info.url}#{symbol_info.symbol_id}",
            description=markdown
        )
        # Show all symbols with the same name that were renamed in the footer.
        embed.set_footer(
            text=", ".join(renamed for renamed in self.renamed_symbols - {symbol} if renamed.endswith(f".{symbol}"))
        )
        return embed

    @commands.group(name='docs', aliases=('doc', 'd'), invoke_without_command=True)
    async def docs_group(self, ctx: commands.Context, *, symbol: Optional[str]) -> None:
        """Look up documentation for Python symbols."""
        await ctx.invoke(self.get_command, symbol=symbol)

    @docs_group.command(name='getdoc', aliases=('g',))
    async def get_command(self, ctx: commands.Context, *, symbol: Optional[str]) -> None:
        """
        Return a documentation embed for a given symbol.

        If no symbol is given, return a list of all available inventories.

        Examples:
            !docs
            !docs aiohttp
            !docs aiohttp.ClientSession
            !docs getdoc aiohttp.ClientSession
        """
        if not symbol:
            inventory_embed = discord.Embed(
                title=f"All inventories (`{len(self.base_urls)}` total)",
                colour=discord.Colour.blue()
            )

            lines = sorted(f"• [`{name}`]({url})" for name, url in self.base_urls.items())
            if self.base_urls:
                await LinePaginator.paginate(lines, ctx, inventory_embed, max_size=400, empty=False)

            else:
                inventory_embed.description = "Hmmm, seems like there's nothing here yet."
                await ctx.send(embed=inventory_embed)

        else:
            symbol = symbol.strip("`")
            # Fetching documentation for a symbol (at least for the first time, since
            # caching is used) takes quite some time, so let's send typing to indicate
            # that we got the command, but are still working on it.
            async with ctx.typing():
                doc_embed = await self.get_symbol_embed(symbol)

            if doc_embed is None:
                symbol = await discord.ext.commands.clean_content().convert(ctx, symbol)
                error_embed = discord.Embed(
                    description=f"Sorry, I could not find any documentation for `{(symbol)}`.",
                    colour=discord.Colour.red()
                )
                error_message = await ctx.send(embed=error_embed)
                await wait_for_deletion(error_message, (ctx.author.id,), timeout=NOT_FOUND_DELETE_DELAY)
                with suppress(discord.NotFound):
                    await ctx.message.delete()
                with suppress(discord.NotFound):
                    await error_message.delete()
            else:
                msg = await ctx.send(embed=doc_embed)
                await wait_for_deletion(msg, (ctx.author.id,))

    @docs_group.command(name='setdoc', aliases=('s',))
    @commands.has_any_role(*MODERATION_ROLES)
    @lock("doc", COMMAND_LOCK_SINGLETON, raise_error=True)
    async def set_command(
        self, ctx: commands.Context, package_name: PackageName,
        base_url: ValidURL, inventory_url: InventoryURL
    ) -> None:
        """
        Adds a new documentation metadata object to the site's database.

        The database will update the object, should an existing item with the specified `package_name` already exist.

        Example:
            !docs setdoc \
                    python \
                    https://docs.python.org/3/ \
                    https://docs.python.org/3/objects.inv
        """
        body = {
            'package': package_name,
            'base_url': base_url,
            'inventory_url': inventory_url
        }
        await self.bot.api_client.post('bot/documentation-links', json=body)

        log.info(
            f"User @{ctx.author} ({ctx.author.id}) added a new documentation package:\n"
            f"Package name: {package_name}\n"
            f"Base url: {base_url}\n"
            f"Inventory URL: {inventory_url}"
        )

        if await self.update_single(package_name, base_url, inventory_url) is None:
            await ctx.send(
                f"Added the package `{package_name}` to the database but failed to fetch inventory; "
                f"trying again in 2 minutes."
            )
            return
        await ctx.send(f"Added package `{package_name}` to database and refreshed inventory.")

    @docs_group.command(name='deletedoc', aliases=('removedoc', 'rm', 'd'))
    @commands.has_any_role(*MODERATION_ROLES)
    @lock("doc", COMMAND_LOCK_SINGLETON, raise_error=True)
    async def delete_command(self, ctx: commands.Context, package_name: PackageName) -> None:
        """
        Removes the specified package from the database.

        Example:
            !docs deletedoc aiohttp
        """
        await self.bot.api_client.delete(f'bot/documentation-links/{package_name}')

        async with ctx.typing():
            # Rebuild the inventory to ensure that everything
            # that was from this package is properly deleted.
            await self.refresh_inventory()
            await doc_cache.delete(package_name)
        await ctx.send(f"Successfully deleted `{package_name}` and refreshed the inventory.")

    @docs_group.command(name="refreshdoc", aliases=("rfsh", "r"))
    @commands.has_any_role(*MODERATION_ROLES)
    @lock("doc", COMMAND_LOCK_SINGLETON, raise_error=True)
    async def refresh_command(self, ctx: commands.Context) -> None:
        """Refresh inventories and show the difference."""
        old_inventories = set(self.base_urls)
        with ctx.typing():
            await self.refresh_inventory()
        new_inventories = set(self.base_urls)

        if added := ", ".join(new_inventories - old_inventories):
            added = "+ " + added

        if removed := ", ".join(old_inventories - new_inventories):
            removed = "- " + removed

        embed = discord.Embed(
            title="Inventories refreshed",
            description=f"```diff\n{added}\n{removed}```" if added or removed else ""
        )
        await ctx.send(embed=embed)

    @docs_group.command(name="cleardoccache")
    @commands.has_any_role(*MODERATION_ROLES)
    async def clear_cache_command(self, ctx: commands.Context, package_name: PackageName) -> None:
        """Clear the persistent redis cache for `package`."""
        if await doc_cache.delete(package_name):
            await ctx.send(f"Successfully cleared the cache for `{package_name}`.")
        else:
            await ctx.send("No keys matching the package found.")
