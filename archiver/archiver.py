import asyncio
import os
import json
import logging

import discord
from discord.ext import commands
from discord.enums import ChannelType
from discord.http import Route

#default logger in case we aren't in a bot, or the bot doesn't have a logger.
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARN)

#tinydb archiver
try:
    from tinydb import TinyDB, Query, where
    from tinydb.storages import JSONStorage
    from tinydb.middlewares import CachingMiddleware
except ImportError as e:
    logger.info('Failed to import TinyDB', exc_info=True)
    TinyDB = False

#mongoDB Archiver
try:
    import motor.motor_asyncio
    Motor = True
except ImportError as e:
    logger.info('Failed to import motor for mongoDB', exc_info=True)
    Motor = False

# Other Backends to Consider:
# * ZODB http://www.zodb.org/en/latest/
# * CodernityDB http://labs.codernity.com/codernitydb/

archive_backends = {}
DEFAULT_PATH = {}
DEFAULT_CONFIG = {'backend': 'TinyDB', 'watch': False }

class Archive:
    
    async def get_all_messages(self):
        #TODO: Deprecate and Remove
        #Implement a db specific version for each db.
        raise NotImplementedError()
    
    async def get_messages(self, user=None, channel=None):
        """Generator over all messages optionally restricted by user or channel. user or 
        channel can be anything with an id attribute, a dictionary containing a 
        key['id'] or just an id."""
        #TODO: Remove after implementing a DB specific version for each DB.
        messages = self.get_all_messages()
        user_id = None
        if hasattr(user, 'id'): user_id = user.id
        elif isinstance(user, dict): user_id = user['id']
        elif user: user_id = user
        
        channel_id = None
        if hasattr(channel, 'id'): channel_id = channel.id
        elif isinstance(channel, dict): channel_id = channel['id']
        elif channel: channel_id = channel
        
        async for m in messages:
            if user_id is not None and user_id != m['author']['id']:
                continue
            if channel_id is not None and channel_id != m['channel_id']:
                continue
            logger.debug("Message search found: %s\n", m)
            yield m
            
    async def add_messages(self, messages, *args, **kwargs):
        """ Adds <messages> to the databese. Returns nuber of messages added """
        return self.add_messages(*args, **kwargs)
    
    def flush(self):
        pass
   
    def close(self):
        pass
   
    def drop(self):
        """ Delete the whole archive. """
        raise NotImplementedError()

if TinyDB:
    #Syntatic sugar for tinydb queries.
    Message = Query()

    class TinyDBArchive(Archive):
        
        DEFAULT_PATH = os.path.join("data", "archiver", "messages.json")
        
        def __init__(self, dbpath, loop=None):
            self.path = dbpath
            # TODO: Not sure to flush cache manualy or detect closing from SIGINT.
            # Can't use Caching safely.(Using it //anyway// because it's just that much faster.)
            self.db = TinyDB(self.path, storage=CachingMiddleware(JSONStorage))
            
        async def get_all_messages(self):
            for m in self.db.all():
                yield m
        
        def zget_messages(self):
            # TODO remove or finish.
            return self.db.search(Message.author.id == member.id)
            msgs = self.db.search(Message.channel_id == channel.id)
            msgs.sort(key=lambda x: x['timestamp'])
            return msgs
            
        def add_messages(self, messages, all_new=False):
            """ Adds <messages> to the database. Returns number of messages added """
            added = 0
            for msg in messages:
                if all_new or not self.db.contains(Message.id  == msg['id']):
                    added += 1
                    self.db.insert(msg)
            return added
        
        async def add_messages(self, messages, all_new=False):
            added = 0
            for msg in messages:
                await asyncio.sleep(0)
                if all_new or not self.db.contains(Message.id  == msg['id']):
                    self.db.insert(msg)
                    added += 1
            return added
        
        def flush(self):
            flushed = False
            if '_storage' in dir(self.db) and 'flush' in dir(self.db._storage):
                self.db._storage.flush()
        
        def drop(self):
            self.db.close()
            os.remove(self.path)

    archive_backends['TinyDB'] = TinyDBArchive
    DEFAULT_PATH['TinyDB'] = os.path.join("data", "archiver", "messages.json")

if Motor:
    class MongoMotorArchive(Archive):
        
        DEFAULT_PATH = "mongodb://localhost/discordArchiver"
        
        def __init__(self, dbpath, loop=None):
            self.client = motor.motor_asyncio.AsyncIOMotorClient(dbpath)
            self.db = self.client.get_database()
        async def add_messages(self, messages, all_new=False):
            #TODO: Batching Might be nice.
            added = 0
            for m in messages:
                m['_id'] = m['id']
                if all_new:
                    added += 1
                    await self.db.messages.insert_one(m)
                else:
                    t = await self.db.messages.find_one({ '_id':m['_id'] })
                    if not t:
                        added += 1
                        await self.db.messages.insert_one(m)
            return added
        async def get_all_messages(self):
            cursor = self.db.messages.find()
            async for m in cursor:
                yield m
                
        def drop(self):
            self.client.drop_database(self.db)
                    
    archive_backends['MongoDB'] = MongoMotorArchive
    DEFAULT_PATH['MongoDB'] = "mongodb://localhost/discordArchiver"
            


class Archiver:
    """Cog that archives messages from discord."""
    
    def __init__(self, bot):
        self.bot = bot
        self.path = os.path.join("data", "archiver")
        os.makedirs(self.path, exist_ok=True)
        self.config_path = os.path.join(self.path, "config.json")
        self._load_config()
        self._save_config()
        self._make_backend()
        
    def _load_config(self):
        "Loads the config from the file to 'self.config' ."
        self.config = DEFAULT_CONFIG.copy()
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r') as config_fp:
                json_config = json.load(config_fp)
            # collections.ChainMap is the normal Way to do this, but json.dump can't handle it (python 3.6.3).
            self.config.update(json_config)
    
    def _save_config(self):
        "Write out 'self.config' to the config file."
        with open(self.config_path, 'w') as config_fp:
            json.dump(self.config, config_fp, indent=4)

    def _make_backend(self):
        if hasattr(self, "archive"):
            self.archive.close()
        backend = self.config['backend']
        self.config[backend+"_path"] = self.config.setdefault(backend+"_path", DEFAULT_PATH[backend])
        self.archive = archive_backends[backend](self.config[backend+"_path"])

    async def _fetch_messages(self, channel, limit=100, before=None, after=None):
        """Use the discord client to get some messages from the provided channel"""
        await asyncio.sleep(0)
        payload = {'limit':limit}
        url = "/channels/{0}/messages?limit="+str(limit)
        if before:
            payload["before"] = before
            url += "&before=" + str(before)
        if after:
            payload['after'] = after
            url += "&after=" + str(after)
        #url = '{0.CHANNELS}/{1}/messages'.format(self.bot.http, channel.id)
        r = Route('GET', url.format(channel.id))
        logger.debug("Fetching messages from '%s' with options: %s", channel, payload)
        #messages = await self.bot.http.get( url, params=payload )
        messages = await self.bot.http.request(r)
        return messages

    async def archive_channel(self, channel):
        """ Gets newest messages from the channel ignoring ones that are already in db.
            Won't update database for deleted or changed messages."""
        added = 0
        # Figure out if we have anything from the channel at all
        messages = self.archive.get_messages(channel=channel)
        try:
            msg1 = await messages.__anext__()
        except StopAsyncIteration:
            logger.debug("we had no messages from channnel: %s", channel)
            messages = await self._fetch_messages(channel)
            if len(messages) > 0:
                added += await self.archive.add_messages(messages, all_new=True)
                msg1 = messages[0]
            else:
                # empty channel. Nothing to do.
                return 0
        
        # Figure out the latest and oldest message we have from channel        
        oldest_id = int(msg1['id'])
        latest_id = oldest_id
        if hasattr(messages, "__iter__"):
            for msg in messages:
                await asyncio.sleep(0)
                oldest_id = min(latest_id, int(msg['id']))
                latest_id = max(latest_id, int(msg['id']))
        else:
           async for msg in messages:
                await asyncio.sleep(0)
                oldest_id = min(latest_id, int(msg['id']))
                latest_id = max(latest_id, int(msg['id']))

        # Get new messages.
        while True:
            messages = await self._fetch_messages(channel, after=latest_id)
            if len(messages) == 0 :
                break
            # added += self.archive.add_messages(messages, all_new=True)
            added +=await  self.archive.add_messages(messages, all_new=True)
            for msg in messages:
                latest_id = max(latest_id, int(msg['id']))

        # Get old messages.
        while True:
            messages = await self._fetch_messages(channel, before=oldest_id)
            if len(messages) == 0 :
                break
            added += await self.archive.add_messages(messages, all_new=True)
            for msg in messages:
                oldest_id = min(oldest_id, int(msg['id']))
        # make sure the archive gets saved to disk.
        self.archive.flush()
        return added
    
    #Methods that start with  "on_" are events in cogs.
    async def on_message(self, message):
        if self.config.get("watch", False):
            await self.archive_channel(message.channel)
    
    @commands.command(pass_context = True)
    async def test_fetch(self, ctx, channel: discord.Channel):
        #channel = self.bot.get_channel(channel_id)
        messages = await self._fetch_messages(channel, limit=11, after="281962999743250452")
        for m in messages:
            print(m['id'])
    
    @commands.group(name="archive", pass_context=True)
    async def _archive(self, ctx):
        """Commands to download messages from discord to a local database."""
        if ctx.invoked_subcommand is None:
            await self.bot.say("See `help %s` for usage info." % ctx.command)
    
    @_archive.command(name="channel",aliases=['chan', 'c'], pass_context = True)
    async def _archive_channel(self, ctx, *channels : str):
        "Download messages from the specified channel to bot's database."
        bot = ctx.bot
        
        if len(channels) == 1 and channels[0] == '*':
            target_channels = bot.get_all_channels()
        else:
            target_channels =  []
            for channel_s in channels:
                channel = bot.get_channel(channel_s)
                if channel is None:
                    await bot.say("Could not find Channel: %s" % channel_s)
                target_channels.append(channel)
        
        for channel in target_channels:
            logger.info("Archiving: %s:%s, %s" % ( channel.id, channel.name, channel.type))
            await bot.say("Archiving Channel %s" % channel)
            if channel.type is ChannelType.text:
                try:
                    added = await self.archive_channel(channel)
                    await bot.say("got %d messages from %s#%s." % (added, channel.server.name, channel.name))
                except Exception as e:
                    logger.warn("Couldn't archive %s because: %s" % (channel.name, repr(e),), exc_info=True)
                    await self.bot.say("Couldn't archive %s because: %s" % (channel.name, repr(e),))
        self.bot.say("Done archiving Everything.")

    @_archive.command(name='config', pass_context=True)
    async def _archive_config(self, ctx, key=None, value=None):
        """usage:
                archive config : Show all settings.
                archive config <key> : Show one setting.
                archive config <key> <value> : Set a setting.(might need to reload to change some)
                archive config reload : reload config from file. 
        """
        if key is None:
            await ctx.bot.say(json.dumps(self.config, indent=4))
        elif key == "reload":
            self._load_config()
            self._make_backend()
        elif value is None:
            await ctx.bot.say("%s == %s" % (key, self.config[key]))
        else:
            if key == "backend":
                if value in archive_backends:
                    self.config[key] = value
                else:
                    await ctx.bot.say("Backend '%s' not installed. Try one of %s" % (value, str(archive_backends.keys())) )
                    return
            self.config[key] = value
            #rebuild backend if relevent options changed.
            if (len(key) > 5 and key[-5:] == "_path") or key == 'backend':
                self._make_backend()
                await ctx.bot.say("Backend Archive reloaded, Note that no data was moved between your backends.")
            #save change.
            self._save_config()

def setup(bot):
    bot.add_cog(Archiver(bot))
