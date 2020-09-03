import asyncio
import logging
import datetime
import time
import typing
import re
from sys import exit

import pymongo
import discord
from discord.ext import commands, tasks

LOG_FORMAT = '%(levelname)s [%(asctime)s]: %(message)s'
logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)

try:
    import config
    import utils#from modules import utils

except ImportError:
    logging.critical('[Bot] config.py does not exist, you should make one from the example config')
    exit(1)

mclient = pymongo.MongoClient(
	config.mongoHost,
	username=config.mongoUser,
	password=config.mongoPass
)
activityStatus = discord.Activity(type=discord.ActivityType.playing, name='DM to contact mods')
bot = commands.Bot(config.command_prefixes, fetch_offline_members=True, activity=activityStatus, case_insensitive=True)

class Mail(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.READY = False
        self.punNames = {
            'tier1': 'T1 Warn',
            'tier2': 'T2 Warn',
            'tier3': 'T3 Warn',
            'clear': 'Warn Clear',
            'mute': 'Mute',
            'unmute': 'Unmute',
            'kick': 'Kick',
            'ban': 'Ban',
            'unban': 'Unban',
            'blacklist': 'Blacklist',
            'unblacklist': 'Unblacklist',
            'note': 'User note'
        }
        self.closeQueue = {}

    @commands.has_any_role(config.modRole)
    @commands.command(name='close')
    async def _close(self, ctx, delay: typing.Optional[str]):
        db = mclient.modmail.logs
        doc = db.find_one({'channel_id': str(ctx.channel.id)})

        if not doc:
            return await ctx.send('This is not a modmail channel!')

        print(self.closeQueue)

        if doc['_id'] in self.closeQueue:
            self.closeQueue[doc['_id']].cancel()

        if delay:
            try:
                delayDate = utils.resolve_duration(delay)
                delayTime = delayDate.timestamp() - datetime.datetime.utcnow().timestamp()

            except KeyError:
                return await ctx.send('Invalid duration')

            print(str(delayDate.timestamp() - datetime.datetime.utcnow().timestamp()))
            event_loop = self.bot.loop
            close_action = event_loop.call_later(delayTime, event_loop.create_task, utils._close_thread(self.bot, ctx, self.modLogs))
            self.closeQueue[doc['_id']] = close_action
            return await ctx.send('Thread scheduled to be closed. Will be closed in ' + utils.humanize_duration(delayDate))

        await utils._close_thread(self.bot, ctx, self.modLogs)

    @commands.has_any_role(config.modRole)
    @commands.command(name='reply', aliases=['r'])
    async def _reply_user(self, ctx, *, content: typing.Optional[str]):
        """
        Reply to an open modmail thread
        """
        await self._reply(ctx, content)

    @commands.has_any_role(config.modRole)
    @commands.command(name='areply', aliases=['ar'])
    async def _reply_anon(self, ctx, *, content: typing.Optional[str]):
        """
        Reply to an open modmail thread anonymously
        """
        await self._reply(ctx, content, True)

    async def _reply(self, ctx, content, anonymous=False):
        db = mclient.modmail.logs
        doc = db.find_one({'channel_id': str(ctx.channel.id)})
        attachments = [x.url for x in ctx.message.attachments]

        if not content and not attachments:
            return await ctx.send('You must provide reply content, attachments, or both to use this command')

        if ctx.channel.category_id != config.category or not doc: # No thread in channel, or not in modmail category
            return await ctx.send('Cannot send a reply here, this is not a modmail channel!')

        if content and len(content) > 1800:
            return await ctx.send(f'Wow there, thats a big reply. Please reduce it by at least {len(content) - 1800} characters')

        if doc['_id'] in self.closeQueue.keys(): # Thread close was scheduled, cancel due to response #TODO
            self.closeQueue[doc['_id']].cancel()
            await ctx.channel.send('Thread closure canceled due to moderator response')

        recipient = doc['recipient']['id']
        member = ctx.guild.get_member(recipient)
        if not member:
            try:
                member = await ctx.guild.fetch_member(recipient)

            except:
                try:
                    member = await self.bot.get_guild(config.appealGuild).fetch_member(recipient)

                except:
                    return await ctx.send('There was an issue replying to this user, they may have left the server')

        try:
            await member.send(f'Reply from **{"Moderator" if anonymous else ctx.author}**: {content if content else ""}')
            if attachments:
                await member.send('\n'.join(attachments))

        except:
            return await ctx.send('There was an issue replying to this user, they may have left the server or disabled DMs')

        db.update_one({'_id': doc['_id']}, {'$push': {'messages': {
            'timestamp': str(ctx.message.created_at),
            'message_id': str(ctx.message.id),
            'content': content if content else '',
            'type': 'thread_message' if not anonymous else 'anonymous',
            'author': {
                'id': str(ctx.author.id),
                'name': ctx.author.name,
                'discriminator': ctx.author.discriminator,
                'avatar_url': str(ctx.author.avatar_url_as(static_format='png', size=1024)),
                'mod': True
            },
            'attachments': attachments
        }}})

        embed = discord.Embed(title='Moderator message', description=content, color=0x7ED321)
        if not anonymous:
            embed.set_author(name=f'{ctx.author} ({ctx.author.id})', icon_url=ctx.author.avatar_url)

        else:
            embed.title = '[ANON] Moderator message'
            embed.set_author(name=f'{ctx.author} ({ctx.author.id}) as r/NintendoSwitch', icon_url='https://cdn.mattbsg.xyz/rns/snoo.png')

        if len(attachments) > 1: # More than one attachment, use fields
            for x in range(len(attachments)):
                embed.add_field(name=f'Attachment {x + 1}', value=attachments[x])

        elif attachments and re.search(r'\.(gif|jpe?g|tiff|png|webp|bmp)$', str(attachments[0]), re.IGNORECASE): # One attachment, image
            embed.set_image(url=attachments[0])

        elif attachments: # Still have an attachment, but not an image
            embed.add_field(name=f'Attachment', value=attachments[0])

        await ctx.send(embed=embed)

    @commands.has_any_role(config.modRole)
    @commands.command(name='open')
    async def _open_thread(self, ctx, member: discord.Member, *, content):
        """
        Open a modmail thread with a user
        """
        if mclient.modmail.logs.find_one({'recipient.id': str(member.id), 'open': True}):
            return await ctx.send(':x: Unable to open modmail to user -- there is already a thread involving them currently open')

        await utils._trigger_create_thread(self.bot, member, ctx.message, open_type='moderator', moderator=ctx.author, content=content, anonymous=False)
        await ctx.send(f':white_check_mark: Modmail has been opened with {member}')

    @commands.has_any_role(config.modRole)
    @commands.command(name='aopen')
    async def _open_thread_anon(self, ctx, member: discord.Member, *, content):
        """
        Open a modmail thread with a user anonymously
        """
        if mclient.modmail.logs.find_one({'recipient.id': str(member.id), 'open': True}):
            return await ctx.send(':x: Unable to open modmail to user -- there is already a thread involving them currently open')

        await utils._trigger_create_thread(self.bot, member, ctx.message, open_type='moderator', moderator=ctx.author, content=content, anonymous=True)
        await ctx.send(f':white_check_mark: Modmail has been opened with {member}')

    @commands.has_any_role(config.modRole)
    @commands.group(name='s', aliases=['snippet', 'snippets'])
    async def _snippets(self, ctx, *args):
        db = mclient.modlog.snippets
        if not args:
            tagList = []
            for x in db.find({}):
                tagList.append(x['_id'])

            embed = discord.Embed(title='Snippet List', description='Here is a list of snippets you can repond with:\n\n' + ', '.join(tagList))
            return await ctx.send(embed=embed)

        doc = db.find_one({'_id': args[0]})

    @commands.Cog.listener()
    async def on_ready(self):
        logging.info('[Bot] Ready')
        if not self.READY:
            self.READY = True
            self.modLogs = self.bot.get_channel(config.modLog)
            self.bot.remove_command('help')

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.errors.CommandNotFound):
            pass # Ignore

        elif isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send(':x: Missing one or more aguments')

        elif isinstance(error, commands.BadArgument):
            return await ctx.send(':x: Invalid argument provided')

        else:
            await ctx.send(':x: An unknown error occured, contact the developer if this continues to happen')
            raise error

    @commands.Cog.listener()
    async def on_typing(self, channel, user, when):
        if channel.type == discord.ChannelType.private:
            db = mclient.modmail.logs
            doc = db.find_one({'open': True, 'creator.id': str(user.id)})
            if doc:
                await self.bot.get_channel(int(doc['channel_id'])).trigger_typing()

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot: return
        attachments = [x.url for x in message.attachments]
        ctx = await self.bot.get_context(message)

        # Do something to check category, and add message to log
        if message.channel.type == discord.ChannelType.private:
            # User has sent a DM -- check 
            db = mclient.modmail.logs
            thread = db.find_one({'recipient.id': str(message.author.id), 'open': True})
            if thread:
                if thread['_id'] in self.closeQueue.keys(): # Thread close was scheduled, cancel due to response
                    self.closeQueue[thread['_id']].cancel()
                    await self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id'])).send('Thread closure canceled due to user response')

                description = message.content if message.content else None
                embed = discord.Embed(title='New message', description=description, color=0x32B6CE)
                embed.set_author(name=f'{message.author} ({message.author.id})', icon_url=message.author.avatar_url)
                embed.set_footer(text=f'{message.channel.id}/{message.id}')

                if len(attachments) > 1: # More than one attachment, use fields
                    for x in range(len(attachments)):
                        embed.add_field(name=f'Attachment {x + 1}', value=attachments[x])

                elif attachments and re.search(r'\.(gif|jpe?g|tiff|png|webp|bmp)$', str(attachments[0]), re.IGNORECASE): # One attachment, image
                    embed.set_image(url=attachments[0])

                elif attachments: # Still have an attachment, but not an image
                    embed.add_field(name=f'Attachment', value=attachments[0])

                await self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id'])).send(embed=embed)
                db.update_one({'_id': thread['_id']}, {'$push': {'messages': {
                    'timestamp': str(message.created_at),
                    'message_id': str(message.id),
                    'content': message.content,
                    'type': 'thread_message',
                    'author': {
                        'id': str(message.author.id),
                        'name': message.author.name,
                        'discriminator': message.author.discriminator,
                        'avatar_url': str(message.author.avatar_url_as(static_format='png', size=1024)),
                        'mod': False
                    },
                    'attachments': attachments
                }}})

            else:
                thread = await utils._trigger_create_thread(self.bot, message.author, message, 'user')
                # TODO: Don't duplicate message embed code based on new thread or just new message
                embed = discord.Embed(title='New message', description=message.content if message.content else None, color=0x32B6CE)
                embed.set_author(name=f'{message.author} ({message.author.id})', icon_url=message.author.avatar_url)
                embed.set_footer(text=f'{message.channel.id}/{message.id}')

                if len(attachments) > 1: # More than one attachment, use fields
                    for x in range(len(attachments)):
                        embed.add_field(name=f'Attachment {x + 1}', value=attachments[x])

                elif attachments and re.search(r'\.(gif|jpe?g|tiff|png|webp|bmp)$', str(attachments[0]), re.IGNORECASE): # One attachment, image
                    embed.set_image(url=attachments[0])

                elif attachments: # Still have an attachment, but not an image
                    embed.add_field(name=f'Attachment', value=attachments[0])

                await thread.send(embed=embed)

            await message.add_reaction('✅')

        elif message.channel.category_id == config.category:
            db = mclient.modmail.logs
            doc = db.find_one({'channel_id': str(message.channel.id)})
            if doc:
                if not ctx.valid: # Not an invoked command, mark as internal message
                    db.update_one({'_id': doc['_id']}, {'$push': {'messages': {
                        'timestamp': str(message.created_at),
                        'message_id': str(message.id),
                        'content': message.content,
                        'type': 'internal',
                        'author': {
                            'id': str(message.author.id),
                            'name': message.author.name,
                            'discriminator': message.author.discriminator,
                            'avatar_url': str(message.author.avatar_url_as(static_format='png', size=1024)),
                            'mod': True
                        },
                        'attachments': attachments
                    }}})

        elif message.channel.type == discord.ChannelType.text and not ctx.guild.get_role(config.modRole) in message.author.roles:
            # Regex to match pings with text content after, and none before. Groups: ping id, text content
            match = re.search(r'^\s*<@!?(\d{15,})>\s+(\S(?:.|\n|\r)+)$', message.content)

            if match and int(match.group(1)) == self.bot.user.id:
                await message.delete()

                db = mclient.modmail.logs
                thread = db.find_one({'recipient.id': str(message.author.id), 'open': True})
                content = match.group(2)
  
                embed = discord.Embed(title='New modmail mention', description=content, color=0x7289DA)
                embed.set_author(name=f'{message.author} ({message.author.id})', icon_url=message.author.avatar_url)
                embed.set_footer(text=f'{message.channel.id}/{message.id}')
                embed.add_field(name=f'Mentioned in', value=f'<#{message.channel.id}> ([Jump to context]({message.jump_url}))')

                try:
                    dm_embed = discord.Embed(description=content, color=0x7289DA)
                    dm_embed.set_author(name=f'{message.author}', icon_url=message.author.avatar_url)
                    dm_message = await message.author.send(f'You mentioned {self.bot.user.name} in <#{message.channel.id}>', embed=dm_embed)
                    await dm_message.add_reaction('✅') # We don't need to do this but it matches the design language

                except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                    await self.bot.get_channel(config.adminChannel).send(f'Cannot create thread for mention from <@{message.author.id}> (failed to send DM)', embed=embed)
                    await message.channel.send(f'<@{message.author.id}> You must open your DMs to use modmail threads. Moderators may still receive your mention.', delete_after=30)

                else:
                    if thread: 
                        channel = self.bot.get_guild(int(thread['guild_id'])).get_channel(int(thread['channel_id']))

                        if thread['_id'] in self.closeQueue.keys(): # Thread close was scheduled, cancel due to response
                                self.closeQueue[thread['_id']].cancel()
                                await channel.send('Thread closure canceled due to user response')

                        db.update_one({'_id': thread['_id']}, {'$push': {'messages': { 
                            'timestamp': str(message.created_at),
                            'message_id': str(message.id),
                            'content': message.content,
                            'type': 'mention',
                            'author': {
                                'id': str(message.author.id),
                                'name': message.author.name,
                                'discriminator': message.author.discriminator,
                                'avatar_url': str(message.author.avatar_url_as(static_format='png', size=1024)),
                                'mod': False
                            },
                            'attachments': attachments,
                            'channel': {
                                'id': message.channel.id,
                                'name': message.channel.name
                            }
                        }}})
        
                    else:
                        channel = await utils._trigger_create_thread(self.bot, message.author, message, 'user', is_mention=True)

                    await channel.send(embed=embed)

bot.add_cog(Mail(bot))
bot.load_extension('jishaku')
bot.run(config.token)
