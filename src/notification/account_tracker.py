import asyncio
import os
import sys
import re
from datetime import datetime, timezone, timedelta

import aiosqlite
import discord
from discord.ext import commands
from tweety import Twitter

from configs.load_configs import configs
from src.log import setup_logger
from src.notification.display_tools import gen_embed, get_action
from src.notification.get_tweets import get_tweets
from src.notification.utils import is_match_media_type, is_match_type, replace_emoji
from src.utils import get_accounts, get_lock
from src.db_function.readonly_db import connect_readonly

EMBED_TYPE = configs['embed']['type'] if configs['embed']['type'] in ['built_in', 'fx_twitter'] else 'built_in'
DOMAIN_NAME = configs['embed']['fx_twitter']['domain_name'] if configs['embed']['fx_twitter']['domain_name'] in ['fxtwitter', 'fixupx'] else 'fxtwitter'

TRIGGER_KEYWORDS = configs.get("keywords_triggering_everyone", ["costco", "queue"])
FORCE_EVERYONE_DEFAULT = configs.get("force_everyone_default", False)

def should_ping_everyone(text: str) -> bool:
    return any(keyword.lower() in text.lower() for keyword in TRIGGER_KEYWORDS)

log = setup_logger(__name__)
lock = get_lock()

class AccountTracker():
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.accounts_data = get_accounts()
        self.db_path = os.path.join(os.getenv('DATA_PATH'), 'tracked_accounts.db')
        self.tweets = {account_name: [] for account_name in self.accounts_data.keys()}
        self.tasksMonitorLogAt = datetime.now(timezone.utc) - timedelta(hours=configs['tasks_monitor_log_period'])
        bot.loop.create_task(self.setup_tasks())

    async def setup_tasks(self):
        async def authenticate_account(account_name, account_token):
            app = Twitter(account_name)
            max_attempts = configs['auth_max_attempts']
            for attempt in range(max_attempts):
                try:
                    await app.load_auth_token(account_token)
                    return app
                except Exception as e:
                    log.error(f"Authentication failed for account: {account_name} [Attempt {attempt + 1}/{max_attempts}]")
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(5)
                    else:
                        log.error(f"Persistent authentication failure for account {account_name}")
                        raise

        for account_name, account_token in self.accounts_data.items():
            try:
                app = await authenticate_account(account_name, account_token)
                self.bot.loop.create_task(self.tweetsUpdater(app)).set_name(f'TweetsUpdater_{account_name}')
            except Exception:
                sys.exit(1)

        async with connect_readonly(self.db_path) as db:
            async with db.execute('SELECT username, client_used FROM user WHERE enabled = 1') as cursor:
                usernames_and_clients = {row[0]: row[1] async for row in cursor}

        for username, client_used in usernames_and_clients.items():
            self.bot.loop.create_task(self.notification(username, client_used)).set_name(username)
        self.bot.loop.create_task(self.tasksMonitor(usernames_and_clients)).set_name('TasksMonitor')

    async def notification(self, username: str, client_used: str):
        while True:
            await asyncio.sleep(configs['tweets_check_period'])

            lastest_tweets = await get_tweets(self.tweets[client_used], username)
            if lastest_tweets is None:
                continue

            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.cursor() as cursor:
                    await cursor.execute('SELECT * FROM user WHERE username = ?', (username,))
                    user = await cursor.fetchone()
                    async with lock:
                        await cursor.execute('UPDATE user SET lastest_tweet = ? WHERE username = ?', (str(lastest_tweets[-1].created_on), username))
                        await db.commit()

                    for tweet in lastest_tweets:
                        log.info(f'find a new tweet from {username}')
                        url = re.sub('twitter', DOMAIN_NAME, tweet.url) if EMBED_TYPE == 'fx_twitter' else tweet.url

                        view, create_view = None, False
                        if bool(tweet.media) and tweet.media[0].type == 'video' and EMBED_TYPE == 'built_in' and configs['embed']['built_in']['video_link_button']:
                            create_view = True
                            button_label, button_url = 'View Video', tweet.media[0].expanded_url
                        elif EMBED_TYPE == 'fx_twitter' and configs['embed']['fx_twitter']['original_url_button']:
                            create_view = True
                            button_label, button_url = 'View Original', tweet.url

                        if create_view:
                            view = discord.ui.View()
                            view.add_item(discord.ui.Button(label=button_label, style=discord.ButtonStyle.link, url=button_url))

                        await cursor.execute('SELECT * FROM notification WHERE user_id = ? AND enabled = 1', (user['id'],))
                        notifications = await cursor.fetchall()
                        for data in notifications:
                            channel = self.bot.get_channel(int(data['channel_id']))
                            if channel is not None and is_match_type(tweet, data['enable_type']) and is_match_media_type(tweet, data['enable_media_type']):
                                try:
                                    mention = f"{channel.guild.get_role(int(data['role_id'])).mention} " if data['role_id'] else ''
                                    if data.get("force_everyone", 0) and should_ping_everyone(tweet.rawContent):
                                        mention = "@everyone "

                                    author, action = tweet.author.name, get_action(tweet)

                                    if not data['customized_msg']:
                                        msg = configs['default_message']
                                    else:
                                        msg = re.sub(r":(\w+):", lambda match: replace_emoji(match, channel.guild), data['customized_msg']) if configs['emoji_auto_format'] else data['customized_msg']
                                    msg = msg.format(mention=mention, author=author, action=action, url=url)

                                    if EMBED_TYPE == 'fx_twitter':
                                        await channel.send(msg, view=view)
                                    else:
                                        footer = 'twitter.png' if configs['embed']['built_in']['legacy_logo'] else 'x.png'
                                        file = discord.File(f'images/{footer}', filename='footer.png')
                                        await channel.send(msg, file=file, embeds=await gen_embed(tweet), view=view)

                                except Exception as e:
                                    if not isinstance(e, discord.errors.Forbidden):
                                        log.error(f'an error occurred at {channel.mention} while sending notification: {e}')

    async def tweetsUpdater(self, app: Twitter):
        updater_name = asyncio.current_task().get_name().split('_', 1)[1]
        while True:
            try:
                self.tweets[updater_name] = await app.get_tweet_notifications()
                await asyncio.sleep(configs['tweets_check_period'])
            except Exception as e:
                log.error(f'{e} (task : tweets updater {updater_name})')
                log.error(f"an unexpected error occurred, try again in {configs['tweets_updater_retry_delay']} minutes")
                await asyncio.sleep(configs['tweets_updater_retry_delay'] * 60)

    async def tasksMonitor(self, users_and_clients: dict[str, str]):
        while True:
            taskSet = {task.get_name() for task in asyncio.all_tasks()}
            users = {username for username, _ in users_and_clients.items()}
            aliveTasks = taskSet & users

            if aliveTasks != users:
                deadTasks = list(users - aliveTasks)
                log.warning(f'dead tasks : {deadTasks}')
                for deadTask in deadTasks:
                    self.bot.loop.create_task(self.notification(deadTask, users_and_clients[deadTask])).set_name(deadTask)
                    log.info(f'restart {deadTask} successfully using {users_and_clients[deadTask]}')

            for client in self.accounts_data.keys():
                if f'TweetsUpdater_{client}' not in taskSet:
                    log.warning(f'tweets updater {client} : dead')

            if (datetime.now(timezone.utc) - self.tasksMonitorLogAt).total_seconds() / 3600 >= configs['tasks_monitor_log_period']:
                log.info(f'alive tasks : {list(aliveTasks)}')
                for client in self.accounts_data.keys():
                    if f'TweetsUpdater_{client}' in taskSet:
                        log.info(f'tweets updater {client} : alive')
                self.tasksMonitorLogAt = datetime.now(timezone.utc)

            await asyncio.sleep(configs['tasks_monitor_check_period'] * 60)

    async def addTask(self, username: str, client_used: str):
        self.bot.loop.create_task(self.notification(username, client_used)).set_name(username)
        log.info(f'new task {username} added successfully using {client_used}')

        for task in asyncio.all_tasks():
            if task.get_name() == 'TasksMonitor':
                try:
                    log.info('existing TasksMonitor has been closed') if task.cancel() else log.info('existing TasksMonitor failed to close')
                except Exception as e:
                    log.warning(f'addTask : {e}')

        async with connect_readonly(self.db_path) as db:
            async with db.execute('SELECT username, client_used FROM user WHERE enabled = 1') as cursor:
                usernames_and_clients = {row[0]: row[1] async for row in cursor}
        self.bot.loop.create_task(self.tasksMonitor(usernames_and_clients)).set_name('TasksMonitor')
        log.info('new TasksMonitor has been started')

    async def removeTask(self, username: str):
        for task in asyncio.all_tasks():
            if task.get_name() == 'TasksMonitor':
                try:
                    log.info('existing TasksMonitor has been closed') if task.cancel() else log.info('existing TasksMonitor failed to close')
                except Exception as e:
                    log.warning(f'removeTask : {e}')

        for task in asyncio.all_tasks():
            if task.get_name() == username:
                try:
                    log.info(f'existing task {username} has been closed') if task.cancel() else log.info(f'existing task {username} failed to close')
                except Exception as e:
                    log.warning(f'removeTask : {e}')

        async with connect_readonly(self.db_path) as db:
            async with db.execute('SELECT username, client_used FROM user WHERE enabled = 1') as cursor:
                usernames_and_clients = {row[0]: row[1] async for row in cursor}
        self.bot.loop.create_task(self.tasksMonitor(usernames_and_clients)).set_name('TasksMonitor')
        log.info('new TasksMonitor has been started')
