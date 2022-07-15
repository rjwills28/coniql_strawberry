import asyncio
from enum import Enum, auto
from typing import Any, AsyncIterator, Dict, Optional

import strawberry
from aiohttp import web
from strawberry.aiohttp.views import GraphQLView
from strawberry.subscriptions import GRAPHQL_TRANSPORT_WS_PROTOCOL, GRAPHQL_WS_PROTOCOL
from strawberry.types import Info

from .caplugin import CAPlugin
from .device_config import ChannelConfig
from .plugin import PluginStore
from .pvaplugin import PVAPlugin
from .simplugin import SimPlugin
from .types import Channel, ChannelValue


# Resolver dependencies
class DeferredChannel:
    id: str
    lock: asyncio.Lock
    channel: Optional[Channel] = None

    async def populate_channel(self) -> Channel:
        raise NotImplementedError(self)

    async def get_channel(self) -> Channel:
        if self.channel is None:
            async with self.lock:
                # If channel is still None we should make it
                if self.channel is None:
                    self.channel = await self.populate_channel()
        assert self.channel
        return self.channel


NO_CONFIG = ChannelConfig(name="")


class GetChannel(DeferredChannel):
    def __init__(self, channel_id: str, timeout: float, store: PluginStore):
        self.plugin, self.config, self.id = store.plugin_config_id(channel_id)
        # Remove the transport prefix from the read pv
        self.pv = store.transport_pv(self.config.read_pv or self.config.write_pv)[1]
        self.timeout = timeout
        self.lock = asyncio.Lock()

    async def populate_channel(self) -> Channel:
        channel = await self.plugin.get_channel(self.pv, self.timeout, self.config)
        return channel


class SubscribeChannel(DeferredChannel):
    def __init__(self, channel_id: str, channel: Channel):
        self.id = channel_id
        self.channel = channel


# Resolvers
async def get_channel(id, timeout, ctx) -> DeferredChannel:
    return GetChannel(id, timeout, ctx["store"])


async def channel_value(parent: DeferredChannel) -> Optional[ChannelValue]:
    channel = await parent.get_channel()
    return channel.get_value()


async def channel_value_float(parent: ChannelValue) -> Optional[float]:
    return parent.formatter.to_float(parent.value)


async def subscribe_channel(id, ctx) -> AsyncIterator[Any]:
    store: PluginStore = ctx["store"]
    plugin, config, channel_id = store.plugin_config_id(id)
    # Remove the transport prefix from the read pv
    pv = store.transport_pv(config.read_pv)[1]
    async for channel in plugin.subscribe_channel(pv, config):
        yield SubscribeChannel(channel_id, channel)
        # yield dict(subscribeChannel=SubscribeChannel(channel_id, channel))


# Schema
@strawberry.type
class Range:
    # "The minimum number that is in this range"
    min: float
    # "The maximum that is in this range"
    max: float


@strawberry.enum
class ChannelQuality(Enum):
    # "Value is known, valid, nothing is wrong"
    VALID = auto()
    # "Value is known, valid, but is in the range generating a warning"
    WARNING = auto()
    # "Value is known, valid, but is in the range generating an alarm condition"
    ALARM = auto()
    # "Value is known, but not valid, e.g. a RW before its first put"
    INVALID = auto()
    # "The value is unknown, for instance because the channel is disconnected"
    UNDEFINED = auto()
    # "The Channel is currently in the process of being changed"
    CHANGING = auto()


@strawberry.type
class ChannelValue:
    # "The current value formatted as a string"
    # @strawberry.field
    # def string(self,
    #    #"Whether to include the units in the string"
    #    units: bool = False) -> str

    # "The current value formatted as a Float, Null if not expressable"
    @strawberry.field
    def float(self) -> float:  # = strawberry.field(resolver=channel_value_float)
        return channel_value_float(self)


@strawberry.type
class ChannelStatus:
    # "Of what quality is the current Channel value"
    quality: ChannelQuality
    # "Free form text describing the current status"
    message: str
    # "Whether the Channel will currently accept mutations"
    mutable: bool


@strawberry.type
class ChannelTime:
    # "Floating point number of seconds since Jan 1, 1970 00:00:00 UTC"
    seconds: float
    # "A more accurate version of the nanoseconds part of the seconds field"
    nanoseconds: int
    # "An integer value whose interpretation is deliberately undefined"
    userTag: int
    # "The timestamp as a datetime object"
    # datetime: datetime


@strawberry.type
class ChannelDisplay:
    # "A human readable possibly multi-line description for a tooltip"
    description: str


@strawberry.type
class Channel:
    # "ID that uniquely defines this Channel, normally a PV"
    id: strawberry.ID

    # "The current value of this channel"
    @strawberry.field
    def value(self) -> ChannelValue:
        return channel_value(self)
    # "When was the value last updated"
    time: ChannelTime
    # "Status of the connection, whether is is mutable, and alarm info"
    status: ChannelStatus
    # "How should the Channel be displayed"
    display: ChannelDisplay


def make_context() -> Dict[str, Any]:
    store = PluginStore()
    store.add_plugin("ssim", SimPlugin())
    store.add_plugin("pva", PVAPlugin())
    store.add_plugin("ca", CAPlugin(), set_default=True)
    context = dict(store=store)
    return context


class MyGraphQLView(GraphQLView):
    async def get_context(self, request: web.Request, response: web.StreamResponse):
        ctx = make_context()
        return {"request": request, "response": response, "ctx": ctx}


@strawberry.type
class Query:
    # "Get the current value of a Channel"
    @strawberry.field
    def getChannel(
        self, info: Info, id: strawberry.ID, timeout: float = 5.0
    ) -> Channel:
        return get_channel(id, timeout, info.context["ctx"])


@strawberry.type
class Subscription:
    # "Subscribe to changes in top level fields of Channel,
    # if they haven't changed they will be Null"
    @strawberry.subscription
    async def subscribeChannel(self, info: Info, id: strawberry.ID) -> Channel:
        return subscribe_channel(id, info.context["ctx"])


def main(args=None) -> None:
    schema = strawberry.Schema(query=Query, subscription=Subscription)

    view = MyGraphQLView(
        schema=schema,
        subscription_protocols=[GRAPHQL_TRANSPORT_WS_PROTOCOL, GRAPHQL_WS_PROTOCOL],
    )

    app = web.Application()

    app.router.add_route("*", "/ws", view)

    web.run_app(app)
