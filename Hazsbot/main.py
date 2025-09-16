# hazsbot_full_with_ship.py  — Hazsbot merged full version (Control Panel + Music Detection + Ship)
# Required environment / secrets: DISCORD_BOT_TOKEN, DEEPSEEK_API_KEY
# Optional environment / secrets: PANEL_GUILD_ID, PANEL_OWNER_ID
# Save this file and run: python3 hazsbot_full_with_ship.py

import os
import json
import random
import asyncio
import time
import platform
import traceback
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

# Optional deps (CPU/Memory & http)
try:
    import psutil  # type: ignore
except Exception:
    psutil = None

try:
    import aiohttp  # for oembed detection
except Exception:
    aiohttp = None

import discord
from discord.ext import commands

from openai import OpenAI

# optional keep-alive helper for Replit (simple Flask ping endpoint)
try:
    from keep_alive import keep_alive
except Exception:
    def keep_alive():
        # noop if keep_alive isn't provided
        return

# ---------------- CONFIG ----------------
VERSION = "1.1.1"  # bumped for ship addition
DATA_FILE = "servers.json"

DISCORD_TOKEN = os.getenv("DISCORDBOTTOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEE_APIKEY")  # required for ?ask

# Panel ownership / binding (set these!)
PANEL_GUILD_ID = int(os.getenv("PANEL_GUILD_ID", "1412743207481118772"))  # control panel server ID
PANEL_OWNER_ID = int(os.getenv("PANEL_OWNER_ID", "1182976165510664202"))  # your user ID

PRESEED_LOG_CHANNELS = {
    "commands": 1412743208827359346,
    "errors":   1412743822231867413,
    "moderation": 1412743856654520360,
    "music": 1412743892134006825,
    "dashboard": 1412743921955639336,
    "joins": 1412746940155691098,
}

DEFAULT_PREFIX = "?"

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

# ---------------- PERSISTENCE ----------------
def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

server_data = load_data()

# ---------------- BOT SETUP ----------------
async def _prefix_callable(bot, message):
    if not message.guild:
        return DEFAULT_PREFIX
    gid = str(message.guild.id)
    if gid in server_data and server_data[gid].get("prefix"):
        return server_data[gid]["prefix"]
    return DEFAULT_PREFIX

bot = commands.Bot(command_prefix=_prefix_callable, intents=intents)
start_time = time.time()

# DeepSeek / OpenRouter client
DEFAULT_MODEL = "deepseek/deepseek-chat-v3.1:free"
client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=DEEPSEEK_API_KEY)

# ---------------- HELPERS ----------------
def ensure_guild(guild_id: int):
    gid = str(guild_id)
    if gid not in server_data:
        server_data[gid] = {}
    g = server_data[gid]
    g.setdefault("warnings", {})
    g.setdefault("mod_roles", [])
    g.setdefault("auto_mod_enabled", True)
    g.setdefault("scheduled_unbans", [])
    g.setdefault("scheduled_unmutes", [])
    g.setdefault("prefix", DEFAULT_PREFIX)
    g.setdefault("categories", {"music": True, "fun": True, "utility": True})
    g.setdefault("welcome_message", "Welcome {user}!")
    g.setdefault("leave_message", "{user} left.")
    g.setdefault("log_channels", {
        "commands": 0,
        "errors": 0,
        "moderation": 0,
        "music": 0,
        "dashboard": 0,
        "joins": 0,
    })
    # pre-seed panel guild channels
    if PANEL_GUILD_ID and guild_id == PANEL_GUILD_ID:
        for k, v in PRESEED_LOG_CHANNELS.items():
            if v and g["log_channels"].get(k) != v:
                g["log_channels"][k] = v
    save_data(server_data)
    return g

last_deleted_message = {}
active_wordles = {}

async def safe_send(destination, content=None, embed=None):
    try:
        if embed:
            return await destination.send(embed=embed)
        else:
            return await destination.send(content)
    except Exception as e:
        print(f"[safe_send] Error sending message: {e}")
        return None

def is_owner_member(member: discord.Member):
    return member.guild and (member.guild.owner_id == member.id)

def is_mod(ctx: commands.Context):
    if not ctx.guild:
        return False
    gid = str(ctx.guild.id)
    ensure_guild(ctx.guild.id)
    mod_roles = server_data[gid]["mod_roles"]
    if not mod_roles:
        return ctx.author.guild_permissions.administrator or is_owner_member(ctx.author)
    if ctx.author.guild_permissions.administrator or is_owner_member(ctx.author):
        return True
    member_role_ids = {r.id for r in ctx.author.roles}
    return any(rid in member_role_ids for rid in mod_roles)

def is_panel_owner_ctx(ctx: commands.Context) -> bool:
    return (
        ctx.guild is not None
        and PANEL_GUILD_ID
        and PANEL_OWNER_ID
        and ctx.guild.id == PANEL_GUILD_ID
        and ctx.author.id == PANEL_OWNER_ID
    )

async def _get_panel_channel(kind: str):
    if not PANEL_GUILD_ID:
        return None
    guild = bot.get_guild(PANEL_GUILD_ID)
    if not guild:
        return None
    ensure_guild(PANEL_GUILD_ID)
    chan_id = server_data[str(PANEL_GUILD_ID)]["log_channels"].get(kind, 0)
    if not chan_id:
        return None
    return guild.get_channel(int(chan_id))

async def log_event(kind: str, content: str, embed: discord.Embed | None = None):
    ch = await _get_panel_channel(kind)
    if not ch:
        return
    try:
        if embed:
            await ch.send(content=content or discord.utils.utcnow().isoformat(), embed=embed)
        else:
            await ch.send(content)
    except Exception as e:
        print(f"[log_event:{kind}] send failed: {e}")

# ---------------- AUTOMOD ----------------
def check_profanity(text: str):
    profanity = {"badword1", "badword2"}
    t = (text or "").lower()
    return any(p in t for p in profanity)

def check_caps(text: str):
    if not text or len(text) < 6:
        return False
    caps = sum(1 for c in text if c.isupper())
    return (caps / max(1, len(text))) > 0.75

def check_invite(text: str):
    t = (text or "").lower()
    return "discord.gg/" in t or "discord.com/invite/" in t

# ---------------- MUSIC LINK DETECTION ----------------
MUSIC_PROVIDERS = {
    'youtube.com':     {'name': 'YouTube', 'oembed': 'https://www.youtube.com/oembed?url={url}&format=json'},
    'youtu.be':        {'name': 'YouTube', 'oembed': 'https://www.youtube.com/oembed?url={url}&format=json'},
    'open.spotify.com':{'name': 'Spotify', 'oembed': 'https://open.spotify.com/oembed?url={url}'},
    'spotify.com':     {'name': 'Spotify', 'oembed': 'https://open.spotify.com/oembed?url={url}'},
    'soundcloud.com':  {'name': 'SoundCloud', 'oembed': 'https://soundcloud.com/oembed?format=json&url={url}'},
    'vimeo.com':       {'name': 'Vimeo', 'oembed': 'https://vimeo.com/api/oembed.json?url={url}'},
    'bandcamp.com':    {'name': 'Bandcamp', 'oembed': 'https://bandcamp.com/oembed?url={url}'},
    'music.apple.com': {'name': 'Apple Music', 'oembed': 'https://music.apple.com/oembed?url={url}'},
}

URL_REGEX = re.compile(r"(https?://[^\s<>]+)")

async def _fetch_oembed(url: str, oembed_template: str, timeout: float = 6.0):
    if not oembed_template or not aiohttp:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            ourl = oembed_template.format(url=url)
            async with session.get(ourl, timeout=timeout) as resp:
                if resp.status == 200:
                    try:
                        return await resp.json()
                    except Exception:
                        return None
    except Exception as e:
        print(f"[_fetch_oembed] failed for {url}: {e}")
    return None

# ---------------- WORDLE ----------------
WORDLE_WORDS = [
"oxide","creek","chair","ocean","amber","drink","stone","blaze","nudge","eagle",
"sable","giant","lofty","noble","nifty","nurse","diner","inbox","zippy","dodge",
"jolly","mirth","valor","magic","irony","woven","kneel","viper","frown","candy",
"yodel","frost","ivory","thorn","wrist","night","vigor","queen","scale","haste",
"prank","vocal","yacht","orbit","spear","heart","grace","entry","mocha","rapid",
"flock","dairy","zilch","opera","globe","mango","olive","hover","piano","knife",
"spice","hatch","hound","jelly","liver","laser","wharf","omega","joker","medal",
"urban","plaza","ultra","honor","crane","pride","grind","novel","index","quest",
"angle","mimic","karma","under","delta","pouch","yummy","zesty","juror","zonal",
"tango","unite","gloom","wrath","tiger","raven","lemon","feast","knees","fable",
"rumor","union","whale","quack","realm","tempo","peace","yield","elbow","itchy",
"acorn","quake","beach","ideal","ember","earth","charm","track","trunk","quill",
"zebra","knack","baker","waltz","blend","apple","flame","brain","joint","vivid",
"river","roast","gamma","shiny","usher","joust","lunch","lapse","youth"
]

# ---------------- EVENTS ----------------
@bot.event
async def on_ready():
    print(f"(＾▽＾) Bot online as {bot.user} — Version {VERSION}")
    print(f"(＾▽＾) Python {platform.python_version()}, discord.py {discord.__version__}")
    print(f"(＾▽＾) DeepSeek model: {DEFAULT_MODEL} | DeepSeek key present: {'yes' if bool(DEEPSEEK_API_KEY) else 'no'}")
    for g in bot.guilds:
        ensure_guild(g.id)
    try:
        embed = discord.Embed(title="Hazsbot Online", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Version", value=VERSION)
        embed.add_field(name="Guilds", value=str(len(bot.guilds)))
        await log_event("dashboard", "✅ Bot is online", embed)
    except Exception:
        pass
    bot.loop.create_task(resume_schedules())

@bot.event
async def on_command(ctx):
    try:
        await log_event(
            "commands",
            f"\u23F0 {datetime.utcnow().isoformat()} | {ctx.author} in #{ctx.channel} (g:{ctx.guild.id if ctx.guild else 'DM'}) ran: {ctx.message.content[:1800]}"
        )
    except Exception:
        pass

@bot.event
async def on_command_error(ctx, error):
    try:
        await safe_send(ctx, f"(･_･;) Error: {error}")
    except Exception:
        pass
    try:
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        snippet = tb[-1900:]
        await log_event("errors", f"\u274C {datetime.utcnow().isoformat()} | {ctx.author} in #{ctx.channel}:\n```py\n{snippet}\n```")
    except Exception:
        pass

@bot.event
async def on_message_delete(message):
    if message.guild and not message.author.bot:
        last_deleted_message[message.channel.id] = {"author": str(message.author), "content": message.content}
    await bot.process_commands(message)

@bot.event
async def on_member_join(member):
    ensure_guild(member.guild.id)
    gconf = server_data[str(member.guild.id)]
    msg = gconf.get("welcome_message") or "Welcome {user}!"
    text = msg.replace("{user}", member.mention).replace("{server}", member.guild.name)
    joins_id = gconf.get("log_channels", {}).get("joins")
    channel = None
    if joins_id:
        channel = member.guild.get_channel(int(joins_id))
    channel = channel or member.guild.system_channel or next((c for c in member.guild.text_channels if c.permissions_for(member.guild.me).send_messages), None)
    if channel:
        await safe_send(channel, f"( ´ ▽ ` )ﾉ {text} — running v{VERSION}")

@bot.event
async def on_member_remove(member):
    ensure_guild(member.guild.id)
    gconf = server_data[str(member.guild.id)]
    msg = gconf.get("leave_message") or "{user} left."
    text = msg.replace("{user}", str(member)).replace("{server}", member.guild.name)
    joins_id = gconf.get("log_channels", {}).get("joins")
    channel = member.guild.get_channel(int(joins_id)) if joins_id else (member.guild.system_channel or None)
    if channel:
        await safe_send(channel, f"(・_・;) {text}")

@bot.event
async def on_message(message):
    # unified on_message: automod + music detection + command processing
    if message.author.bot:
        return
    if not message.guild:
        await bot.process_commands(message)
        return
    gdata = ensure_guild(message.guild.id)
    if gdata.get("auto_mod_enabled", True):
        if check_profanity(message.content):
            try:
                await message.delete()
                await safe_send(message.channel, f"(╯︵╰,) {message.author.mention}, your message was removed for profanity.")
            except Exception as e:
                print(f"[automod] delete/send failed: {e}")
            return
        if check_caps(message.content):
            try:
                await message.delete()
                await safe_send(message.channel, f"(¬_¬) {message.author.mention}, please avoid excessive caps.")
            except Exception as e:
                print(f"[automod] delete/send failed: {e}")
            return
        if check_invite(message.content):
            try:
                await message.delete()
                await safe_send(message.channel, f"(・_・;) {message.author.mention}, invite links are not allowed here.")
            except Exception as e:
                print(f"[automod] delete/send failed: {e}")
            return

    # music link detection
    try:
        urls = URL_REGEX.findall(message.content or "")
        if urls:
            for url in urls:
                parsed = urlparse(url)
                host = parsed.netloc.lower()
                if host.startswith("www."):
                    host = host[4:]
                for domain, provider in MUSIC_PROVIDERS.items():
                    if host == domain or host.endswith('.' + domain):
                        oembed_data = await _fetch_oembed(url, provider.get('oembed'))
                        embed = discord.Embed(title=f"{provider['name']} link detected", url=url, color=discord.Color.purple(), timestamp=discord.utils.utcnow())
                        if oembed_data:
                            title = oembed_data.get('title') or oembed_data.get('name')
                            author = oembed_data.get('author_name') or oembed_data.get('provider_name')
                            thumb = oembed_data.get('thumbnail_url') or oembed_data.get('thumbnail')
                            if title:
                                embed.add_field(name='Title', value=title[:1024], inline=False)
                            if author:
                                embed.add_field(name='Author', value=author[:1024], inline=True)
                            if thumb:
                                embed.set_thumbnail(url=thumb)
                        await log_event('music', f"{message.author} posted a {provider['name']} link: {url}", embed)
                        try:
                            await message.add_reaction("\U0001F3B5")
                        except Exception:
                            pass
                        break
    except Exception as e:
        print(f"[on_message music detect] {e}")

    await bot.process_commands(message)

# ---------------- AI ASK ----------------
async def get_ai_response(prompt, max_tokens=800, temperature=0.7):
    if not DEEPSEEK_API_KEY:
        return "(⚠) No DeepSeek API key configured. Set DEEPSEEK_API_KEY environment variable."
    try:
        loop = asyncio.get_event_loop()
        completion = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=DEFAULT_MODEL,
                messages=[
                    {"role": "system", "content": "You are Hazsbot, a helpful assistant (｡◕‿◕｡)."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=min(800, max_tokens),
                temperature=temperature,
                extra_headers={"HTTP-Referer": os.getenv("SITE_URL", "https://example.com"), "X-Title": os.getenv("SITE_TITLE", "Hazsbot")},
                extra_body={},
                timeout=30,
            )
        )
        return completion.choices[0].message.content.strip()
    except asyncio.TimeoutError:
        print("[get_ai_response] DeepSeek request timed out")
        return "(･_･;) DeepSeek request timed out."
    except Exception as e:
        print(f"[get_ai_response] OpenRouter/OpenAI client error: {e}")
        return f"(･_･;) DeepSeek request failed: {e}"

@bot.command(name="ask")
async def cmd_ask(ctx, *, question: str = None):
    if not question:
        return await safe_send(ctx, "(･_･) Please provide a question after `?ask`.")
    if ctx.guild is None:
        return await safe_send(ctx, "(･_･) The ask command only works in servers (no DMs).")
    async with ctx.typing():
        try:
            answer = await get_ai_response(question)
            if not answer:
                return await safe_send(ctx, "(･_･;) AI returned no response.")
            for i in range(0, len(answer), 1900):
                await safe_send(ctx, answer[i:i+1900])
            print(f"[ask] {ctx.author} asked: {question}")
        except Exception as e:
            print(f"[ask] Exception: {e}")
            await safe_send(ctx, f"(･_･;) Error contacting AI: {e}")

# ---------------- Panel decorator ----------------
def panel_only():
    def predicate(ctx: commands.Context):
        if not is_panel_owner_ctx(ctx):
            raise commands.CheckFailure("Not authorized for panel.")
        return True
    return commands.check(predicate)

# ---------------- Commands (version/test/dashboard etc.) ----------------
@bot.command(name="version")
async def cmd_version(ctx):
    uptime = int(time.time() - start_time)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)

    moderation = [
        "`?addmodrole @role` - add a mod role (admin only)",
        "`?removemodrole @role` - remove mod role (admin only)",
        "`?warn @user <reason>` - warn a user",
        "`?unwarn @user [index]` - remove a warning",
            "`?list warnings|modroles` - list warns / mod roles",
        "`?ban @user [minutes] <reason>` - ban (temp or perm)",
        "`?kick @user <reason>` - kick user",
        "`?mute @user [minutes] <reason>` - mute temporarily",
    ]
    fun = [
        "`?wordle` / `?guess <word>` - play Wordle",
        "`?rps <rock|paper|scissors>` - Rock Paper Scissors",
        "`?dice <max>` - roll 1..max",
        "`?coinflip` - flip a coin",
        "`?ask <question>` - ask the AI (server only)",
        "`?ship @user1 @user2` - deterministic compatibility (non-random)"
    ]
    utility = [
        "`?snipe` - show last deleted message",
        "`?pin <id>` / `?unpin <id>` / `?bulkpin <limit>`",
        "`?setslowmode <seconds>` - set slowmode",
        "`?remindme <10s|5m|2h> <msg>` - reminder",
        "`?userinfo [@user]` - user info",
        "`?avatar [@user]` - show avatar",
        "`?test` - run diagnostics",
    ]
    music = [
        "`?WIP` - dude idk WIP",
    ]
    panel = [
        "`?dashboard` - control panel (owner only)",
        "`?setlogchannel <type> #channel` - set log channel",
        "`?setprefix <prefix>` - set command prefix",
        "`?togglecategory <music|fun|utility>` - enable/disable features",
        "`?setwelcome <msg>` / `?setleave <msg>` - welcome/leave messages",    
    ]

    embed = discord.Embed(title=f"Bot v{VERSION}", color=discord.Color.blue())
    embed.add_field(name="Uptime", value=f"{h}h {m}m {s}s", inline=True)
    embed.add_field(name="Guilds", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Python/discord.py", value=f"{platform.python_version()} / {discord.__version__}", inline=False)
    embed.add_field(name="Moderation", value="\n".join(moderation), inline=False)
    embed.add_field(name="Fun", value="\n".join(fun), inline=False)
    embed.add_field(name="Utility", value="\n".join(utility), inline=False)
    embed.add_field(name="Music", value="\n".join(music), inline=False)
    embed.add_field(name="Panel (Control Server)", value="\n".join(panel), inline=False)

    await safe_send(ctx, embed=embed)

@bot.command(name="test")
async def cmd_test(ctx):
    results = []
    results.append("✅ DeepSeek key present" if DEEPSEEK_API_KEY else "(⚠) DeepSeek key missing — `?ask` will not work")
    try:
        _ = server_data is not None
        results.append("✅ Persistent storage accessible")
    except Exception as e:
        results.append(f"❌ Storage error: {e}")
    try:
        await safe_send(ctx, "(＾▽＾) Test message: sending works")
        results.append("✅ Sending messages OK")
    except Exception as e:
        results.append(f"❌ Sending failed: {e}")
    results.append("✅ Snipe subsystem ready" if isinstance(last_deleted_message, dict) else "❌ Snipe subsystem problem")
    results.append("✅ Scheduling/persistence expected to work (temp unban/unmute persisted)")
    try:
        mod_ok = is_mod(ctx)
        results.append("✅ Mod-check logic OK" if isinstance(mod_ok, bool) else "❌ Mod-check problem")
    except Exception as e:
        results.append(f"❌ Mod-check error: {e}")
    await safe_send(ctx, "(｡◕‿◕｡) Test results:\n" + "\n".join(results))

# ---------------- Panel commands ----------------
@bot.command(name="dashboard")
@panel_only()
async def cmd_dashboard(ctx):
    uptime = int(time.time() - start_time)
    h, rem = divmod(uptime, 3600)
    m, s = divmod(rem, 60)
    latency_ms = int(bot.latency * 1000)
    cpu = mem_used = mem_total = mem_pct = "N/A"
    try:
        if psutil:
            cpu = f"{psutil.cpu_percent(interval=0.3)}%"
            vm = psutil.virtual_memory()
            mem_used = f"{vm.used // (1024*1024)} MB"
            mem_total = f"{vm.total // (1024*1024)} MB"
            mem_pct = f"{vm.percent}%"
    except Exception:
        pass
    guilds = len(bot.guilds)
    members = sum((g.member_count or 0) for g in bot.guilds)
    voices = len(bot.voice_clients)
    embed = discord.Embed(title="Hazsbot Dashboard", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    embed.add_field(name="Status", value=f"Online | Ping {latency_ms}ms", inline=False)
    embed.add_field(name="Uptime", value=f"{h}h {m}m {s}s", inline=True)
    embed.add_field(name="Guilds", value=str(guilds), inline=True)
    embed.add_field(name="Members", value=str(members), inline=True)
    embed.add_field(name="Voice Conns", value=str(voices), inline=True)
    embed.add_field(name="CPU", value=str(cpu), inline=True)
    embed.add_field(name="Memory", value=f"{mem_used}/{mem_total} ({mem_pct})", inline=True)
    await safe_send(ctx, embed=embed)
    await log_event("dashboard", "📊 Dashboard requested", embed)

@bot.command(name="setlogchannel")
@panel_only()
async def cmd_setlogchannel(ctx, kind: str, channel: discord.TextChannel):
    kind = kind.lower()
    valid = {"commands", "errors", "moderation", "music", "dashboard", "joins"}
    if kind not in valid:
        return await safe_send(ctx, f"(･_･;) kind must be one of: {', '.join(sorted(valid))}")
    if ctx.guild.id != PANEL_GUILD_ID:
        return await safe_send(ctx, "(･_･;) Must run in the panel server.")
    g = ensure_guild(PANEL_GUILD_ID)
    g["log_channels"][kind] = channel.id
    save_data(server_data)
    await safe_send(ctx, f"(＾▽＾) Set **{kind}** logs to {channel.mention}.")

@bot.command(name="setprefix")
@commands.has_permissions(administrator=True)
async def cmd_setprefix(ctx, prefix: str):
    g = ensure_guild(ctx.guild.id)
    if not prefix or len(prefix) > 5:
        return await safe_send(ctx, "(･_･;) Prefix must be 1–5 chars.")
    g["prefix"] = prefix
    save_data(server_data)
    await safe_send(ctx, f"(＾▽＾) Prefix set to `{prefix}` for this server.")

@bot.command(name="togglecategory")
@commands.has_permissions(administrator=True)
async def cmd_togglecategory(ctx, category: str):
    category = category.lower()
    if category not in ("music", "fun", "utility"):
        return await safe_send(ctx, "(･_･;) Use music, fun, or utility.")
    g = ensure_guild(ctx.guild.id)
    curr = bool(g["categories"].get(category, True))
    g["categories"][category] = not curr
    save_data(server_data)
    await safe_send(ctx, f"(＾▽＾) Category **{category}** is now {'enabled' if not curr else 'disabled'}.")

@bot.command(name="setwelcome")
@commands.has_permissions(administrator=True)
async def cmd_setwelcome(ctx, *, message: str):
    g = ensure_guild(ctx.guild.id)
    g["welcome_message"] = message
    save_data(server_data)
    await safe_send(ctx, f"(＾▽＾) Updated welcome message.")

@bot.command(name="setleave")
@commands.has_permissions(administrator=True)
async def cmd_setleave(ctx, *, message: str):
    g = ensure_guild(ctx.guild.id)
    g["leave_message"] = message
    save_data(server_data)
    await safe_send(ctx, f"(＾▽＾) Updated leave message.")

# ---------------- Moderation commands (continued in next section) ----------------
# Warns, ban, kick, mute, pins, slowmode, audit are implemented below.

# Warns
def add_warning(guild_id: int, user_id: int, reason: str):
    gid = str(guild_id)
    ensure_guild(guild_id)
    warns = server_data[gid]["warnings"]
    warns.setdefault(str(user_id), []).append({"reason": reason, "when": datetime.utcnow().isoformat()})
    save_data(server_data)

def remove_warning(guild_id: int, user_id: int, index: int = None):
    gid = str(guild_id)
    ensure_guild(guild_id)
    warns = server_data[gid]["warnings"].get(str(user_id), [])
    if not warns:
        return None
    if index is None:
        return warns.pop()
    idx0 = index - 1
    if 0 <= idx0 < len(warns):
        return warns.pop(idx0)
    return None

@bot.command(name="warn")
async def cmd_warn(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    if not is_mod(ctx):
        return await safe_send(ctx, "(╯︵╰,) You do not have permission to warn members.")
    add_warning(ctx.guild.id, member.id, reason)
    await safe_send(ctx, f"(｀・ω・´) {member.mention} warned: {reason}")
    await log_event("moderation", f"⚠️ {ctx.author} warned {member} in {ctx.guild.name}: {reason}")

@bot.command(name="unwarn")
async def cmd_unwarn(ctx, member: discord.Member, index: int = None):
    if not is_mod(ctx):
        return await safe_send(ctx, "(╯︵╰,) You do not have permission to unwarn members.")
    removed = remove_warning(ctx.guild.id, member.id, index)
    if removed:
        await safe_send(ctx, f"(＾▽＾) Removed warn: {removed['reason']}")
        await log_event("moderation", f"🗑️ {ctx.author} removed a warn for {member} in {ctx.guild.name}: {removed['reason']}")
    else:
        await safe_send(ctx, "(･_･) No warn found or invalid index.")

@bot.command(name="ban")
async def cmd_ban(ctx, member: discord.Member, duration_minutes: int = 0, *, reason: str = "No reason provided"):
    if not is_mod(ctx):
        return await safe_send(ctx, "(╯︵╰,) You do not have permission to ban members.")
    try:
        await ctx.guild.ban(member, reason=reason, delete_message_days=0)
        await safe_send(ctx, f"(｀・ω・´) {member} banned. Reason: {reason} {'(temporarily)' if duration_minutes>0 else '(permanent)'}")
        await log_event("moderation", f"⛔ {ctx.author} banned {member} in {ctx.guild.name} ({'temp ' + str(duration_minutes) + 'm' if duration_minutes>0 else 'perm'}): {reason}")
        if duration_minutes > 0:
            unban_at = datetime.utcnow() + timedelta(minutes=duration_minutes)
            bot.loop.create_task(schedule_unban(ctx.guild.id, member.id, unban_at))
    except Exception as e:
        await safe_send(ctx, f"(･_･;) Failed to ban: {e}")

@bot.command(name="kick")
async def cmd_kick(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    if not is_mod(ctx):
        return await safe_send(ctx, "(╯︵╰,) You do not have permission to kick members.")
    try:
        await member.kick(reason=reason)
        await safe_send(ctx, f"(｀・ω・´) {member} kicked. Reason: {reason}")
        await log_event("moderation", f"👢 {ctx.author} kicked {member} in {ctx.guild.name}: {reason}")
    except Exception as e:
        await safe_send(ctx, f"(･_･;) Failed to kick: {e}")

@bot.command(name="mute")
async def cmd_mute(ctx, member: discord.Member, minutes: int = 10, *, reason: str = "No reason provided"):
    if not is_mod(ctx):
        return await safe_send(ctx, "(╯︵╰,) You do not have permission to mute members.")
    guild = ctx.guild
    mute_role = discord.utils.get(guild.roles, name="Muted")
    try:
        if not mute_role:
            mute_role = await guild.create_role(name="Muted", reason="Created by bot for mute command")
            for ch in guild.channels:
                try:
                    await ch.set_permissions(mute_role, send_messages=False, speak=False, add_reactions=False)
                except Exception:
                    pass
        await member.add_roles(mute_role, reason=reason)
        await safe_send(ctx, f"(ﾉ◕‿◕) {member.mention} muted for {minutes} minute(s). Reason: {reason}")
        await log_event("moderation", f"🔇 {ctx.author} muted {member} for {minutes}m in {ctx.guild.name}: {reason}")
        unmute_at = datetime.utcnow() + timedelta(minutes=minutes)
        bot.loop.create_task(schedule_unmute(guild.id, member.id, mute_role.id, unmute_at))
    except Exception as e:
        await safe_send(ctx, f"(･_･;) Could not mute: {e}")

# Pins / Slowmode / Bulkpin
@bot.command(name="pin")
async def cmd_pin(ctx, message_id: int):
    if not is_mod(ctx):
        return await safe_send(ctx, "(╯︵╰,) You do not have permission to pin messages.")
    try:
        m = await ctx.channel.fetch_message(message_id)
        await m.pin()
        await safe_send(ctx, f"( ^_^)／ Message {message_id} pinned.")
    except Exception as e:
        await safe_send(ctx, f"(･_･;) Could not pin message: {e}")

@bot.command(name="unpin")
async def cmd_unpin(ctx, message_id: int):
    if not is_mod(ctx):
        return await safe_send(ctx, "(╯︵╰,) You do not have permission to unpin messages.")
    try:
        m = await ctx.channel.fetch_message(message_id)
        await m.unpin()
        await safe_send(ctx, f"( ^_^)／ Message {message_id} unpinned.")
    except Exception as e:
        await safe_send(ctx, f"(･_･;) Could not unpin message: {e}")

@bot.command(name="bulkpin")
async def cmd_bulkpin(ctx, limit: int = 10):
    if not is_mod(ctx):
        return await safe_send(ctx, "(╯︵╰,) You do not have permission to bulk pin.")
    pinned = 0
    try:
        async for m in ctx.channel.history(limit=limit):
            if not m.pinned:
                await m.pin()
                pinned += 1
        await safe_send(ctx, f"(＾▽＾) Pinned {pinned} messages.")
    except Exception as e:
        await safe_send(ctx, f"(･_･;) Bulk pin failed: {e}")

@bot.command(name="setslowmode")
async def cmd_setslowmode(ctx, seconds: int):
    if not is_mod(ctx):
        return await safe_send(ctx, "(╯︵╰,) You do not have permission to set slowmode.")
    if seconds < 0 or seconds > 21600:
        return await safe_send(ctx, "(･_･;) Slowmode must be 0-21600 seconds.")
    try:
        await ctx.channel.edit(slowmode_delay=seconds)
        await safe_send(ctx, f"(⌛) Slowmode set to {seconds} second(s).")
    except Exception as e:
        await safe_send(ctx, f"(･_･;) Could not set slowmode: {e}")

@bot.command(name="audit")
@commands.has_permissions(administrator=True)
async def cmd_audit(ctx, action: str = None):
    if action not in ("bans", "kicks", None):
        return await safe_send(ctx, "(¬_¬) Use `?audit bans` or `?audit kicks`.")
    entries = []
    try:
        async for entry in ctx.guild.audit_logs(limit=20):
            if action is None:
                entries.append(f"{entry.action.name} | {entry.target} by {entry.user} at {entry.created_at.isoformat()}")
            elif action == "bans" and entry.action.name.lower() == "ban":
                entries.append(f"{entry.target} banned by {entry.user} at {entry.created_at.isoformat()}")
            elif action == "kicks" and entry.action.name.lower() == "kick":
                entries.append(f"{entry.target} kicked by {entry.user} at {entry.created_at.isoformat()}")
        if not entries:
            return await safe_send(ctx, "(･_･) No audit entries found.")
        for i in range(0, len(entries), 8):
            await safe_send(ctx, "```\n" + "\n".join(entries[i:i+8]) + "\n```")
    except Exception as e:
        await safe_send(ctx, f"(･_･;) Could not fetch audit logs: {e}")

# Games & utilities
@bot.command(name="dice")
async def cmd_dice(ctx, max_number: int = 6):
    if max_number < 1:
        return await safe_send(ctx, "(･_･;) Enter a positive number.")
    r = random.randint(1, max_number)
    await safe_send(ctx, f"(⌐■_■) Rolled: {r} (1-{max_number})")

@bot.command(name="coinflip")
async def cmd_coinflip(ctx):
    res = random.choice(["Heads", "Tails"])
    await safe_send(ctx, f"(＾▽＾) {ctx.author.mention} flipped: **{res}**")

@bot.command(name="rps")
async def cmd_rps(ctx, choice: str):
    choice = choice.lower()
    if choice not in ("rock", "paper", "scissors"):
        return await safe_send(ctx, "(･_･;) Use rock, paper, or scissors.")
    bot_choice = random.choice(["rock", "paper", "scissors"])
    if choice == bot_choice:
        result = "It's a tie!"
    elif (choice, bot_choice) in {("rock","scissors"),("scissors","paper"),("paper","rock")}:
        result = "You win!"
    else:
        result = "You lose!"
    await safe_send(ctx, f"(＾▽＾) You: {choice} | Bot: {bot_choice} → {result}")

# ---------------- Ship command (deterministic, non-random) ----------------
def _letters_similarity_score(name1: str, name2: str) -> float:
    # returns 0..20
    a = re.sub("[^a-z]", "", (name1 or "").lower())
    b = re.sub("[^a-z]", "", (name2 or "").lower())
    sa = set(a)
    sb = set(b)
    if not sa and not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb) or 1
    return (inter / union) * 20.0

def _mutual_guilds_score(user1_id: int, user2_id: int) -> float:
    # returns 0..20 — count mutual guilds (cap at 5 for score scaling)
    count = 0
    for g in bot.guilds:
        try:
            if g.get_member(user1_id) and g.get_member(user2_id):
                count += 1
        except Exception:
            continue
    capped = min(count, 5)
    return (capped / 5.0) * 20.0

def _role_overlap_score(member1: discord.Member | None, member2: discord.Member | None, current_guild: discord.Guild) -> float:
    # returns 0..30 — only calculated if both are members of current guild
    if not isinstance(member1, discord.Member) or not isinstance(member2, discord.Member):
        return 0.0
    if member1.guild != current_guild or member2.guild != current_guild:
        return 0.0
    roles1 = {r.id for r in member1.roles if r != current_guild.default_role}
    roles2 = {r.id for r in member2.roles if r != current_guild.default_role}
    union = roles1 | roles2
    if not union:
        return 0.0
    inter = len(roles1 & roles2)
    return (inter / len(union)) * 30.0

def _account_age_score(u1, u2) -> float:
    # returns 0..15 — higher if account creation dates are close
    if not hasattr(u1, "created_at") or not hasattr(u2, "created_at"):
        return 0.0
    diff_days = abs((u1.created_at - u2.created_at).days)
    # If created within 30 days -> full 15, within a year -> scaled down, otherwise smaller
    if diff_days <= 30:
        return 15.0
    if diff_days <= 365:
        return 15.0 * (1 - (diff_days - 30) / (365 - 30))
    # older difference: fade to 0 at 3 years
    if diff_days <= (365 * 3):
        return 15.0 * (1 - (diff_days - 365) / (365 * 2))
    return 0.0

def _discriminator_score(u1, u2) -> float:
    # returns 0..5
    d1 = getattr(u1, "discriminator", None)
    d2 = getattr(u2, "discriminator", None)
    if d1 and d2 and d1 == d2:
        return 5.0
    return 0.0

def compute_ship_score(u1, u2, ctx_guild: discord.Guild | None):
    # Sum components
    name_score = _letters_similarity_score(getattr(u1, "name", getattr(u1, "display_name", str(u1))),
                                          getattr(u2, "name", getattr(u2, "display_name", str(u2))))
    mutual_score = _mutual_guilds_score(int(u1.id), int(u2.id))
    role_score = _role_overlap_score(u1 if isinstance(u1, discord.Member) else None,
                                    u2 if isinstance(u2, discord.Member) else None,
                                    ctx_guild) if ctx_guild else 0.0
    age_score = _account_age_score(u1, u2)
    disc_score = _discriminator_score(u1, u2)
    total = name_score + mutual_score + role_score + age_score + disc_score
    total = max(0.0, min(100.0, total))
    # Round values
    breakdown = {
        "name_letters": round(name_score, 2),
        "mutual_guilds": round(mutual_score, 2),
        "role_overlap": round(role_score, 2),
        "account_age": round(age_score, 2),
        "discriminator": round(disc_score, 2),
        "total": round(total, 2)
    }
    return breakdown

@bot.command(name="ship")
async def cmd_ship(ctx, user1: discord.User, user2: discord.User):
    """
    Deterministic 'ship' command — not random.
    Usage: ?ship @user1 @user2
    """
    try:
        # compute
        breakdown = compute_ship_score(user1, user2, ctx.guild)
        score = breakdown["total"]
        # friendly verdict
        if score >= 85:
            verdict = "wowzers yar guds ‹𝟹"
        elif score >= 65:
            verdict = "yeh pretty gud ദ്ദി(˵ •̀ ᴗ - ˵ ) ✧"
        elif score >= 45:
            verdict = "well it could work ( ⸝⸝´꒳`⸝⸝)"
        elif score >= 25:
            verdict = "not gud not bad idk (ᵕ—ᴗ—)"
        else:
            verdict = "not gud (ᵕ—ᴗ—)"

        # Compose ship name (deterministic): merge first halves of display names
        def _clean(name):
            return re.sub(r"\s+", "", str(name))
        n1 = _clean(user1.display_name if isinstance(user1, discord.Member) else getattr(user1, "name", str(user1)))
        n2 = _clean(user2.display_name if isinstance(user2, discord.Member) else getattr(user2, "name", str(user2)))
        shipname = (n1[:max(1, len(n1)//2)] + n2[max(1, len(n2)//2):])[:24]  # cap length

        embed = discord.Embed(title=f"Ship: {user1} <3 {user2}", color=discord.Color.magenta(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Score", value=f"**{score:.2f}%** — {verdict}", inline=False)
        embed.add_field(name="Ship name", value=shipname or "NoName", inline=True)
        embed.add_field(name="Breakdown", value=(
            f"Name letters: {breakdown['name_letters']}\n"
            f"Mutual guilds: {breakdown['mutual_guilds']}\n"
            f"Role overlap: {breakdown['role_overlap']}\n"
            f"Account age: {breakdown['account_age']}\n"
            f"Discriminator: {breakdown['discriminator']}"
        ), inline=False)
        await safe_send(ctx, embed=embed)
    except Exception as e:
        await safe_send(ctx, f"(･_･;) Could not compute ship: {e}")

# ---------------- Games continued (Wordle etc.) ----------------
@bot.command(name="wordle")
async def cmd_wordle(ctx):
    word = random.choice(WORDLE_WORDS)
    active_wordles[str(ctx.author.id)] = {"word": word, "attempts": 0}
    await safe_send(ctx, "(≧◡≦) Wordle started! Guess with `?guess <word>` — 6 attempts total.")

@bot.command(name="guess")
async def cmd_guess(ctx, guess: str):
    uid = str(ctx.author.id)
    if uid not in active_wordles:
        return await safe_send(ctx, "(･_･) You don't have an active Wordle. Use `?wordle`.")
    state = active_wordles[uid]
    target = state["word"]
    attempts = state["attempts"]
    if attempts >= 6:
        del active_wordles[uid]
        return await safe_send(ctx, f"(｡•́︿•̀｡) Out of attempts. The word was **{target}**.")
    guess = guess.lower()
    if len(guess) != 5:
        return await safe_send(ctx, "(･_･;) Guess must be 5 letters.")
    feedback = []
    for i in range(5):
        if guess[i] == target[i]:
            feedback.append("🟩")
        elif guess[i] in target:
            feedback.append("🟨")
        else:
            feedback.append("⬛")
    state["attempts"] += 1
    if guess == target:
        del active_wordles[uid]
        return await safe_send(ctx, f"(ﾉ◕ヮ◕)ﾉ You guessed it! The word was **{target}**.")
    if state["attempts"] >= 6:
        del active_wordles[uid]
        return await safe_send(ctx, f"(｡•́︿•̀｡) Out of attempts. The word was **{target}**.")
    await safe_send(ctx, f"{''.join(feedback)} — Attempts used: {state['attempts']}/6")

# Snipe
@bot.command(name="snipe")
async def cmd_snipe(ctx):
    data = last_deleted_message.get(ctx.channel.id)
    if not data:
        return await safe_send(ctx, "(・_・;) Nothing to snipe.")
    await safe_send(ctx, f"(¬‿¬) Last deleted by {data['author']}: {data['content']}")

# Remindme
@bot.command(name="remindme")
async def cmd_remindme(ctx, when: str, *, message: str):
    unit = when[-1]
    num = when[:-1]
    if not num.isdigit() or unit not in ("s","m","h"):
        return await safe_send(ctx, "(･_･;) Use format like 10s, 5m, 2h.")
    num = int(num)
    seconds = num if unit == "s" else num*60 if unit == "m" else num*3600
    await safe_send(ctx, f"(＾▽＾) Reminder set. I will remind you in {when}.")
    async def do_remind():
        await asyncio.sleep(seconds)
        try:
            await safe_send(ctx.author, f"(🔔) Reminder: {message}")
        except Exception:
            try:
                await safe_send(ctx, f"(🔔) Reminder for {ctx.author.mention}: {message}")
            except Exception as e:
                print(f"[remindme] failed to deliver reminder: {e}")
    bot.loop.create_task(do_remind())

# Userinfo / Avatar
@bot.command(name="userinfo")
async def cmd_userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    created = member.created_at.strftime("%Y-%m-%d %H:%M:%S")
    joined = member.joined_at.strftime("%Y-%m-%d %H:%M:%S") if member.joined_at else "Unknown"
    roles = [r.name for r in member.roles if (ctx.guild and r != ctx.guild.default_role) or (not ctx.guild)]
    perms = []
    try:
        perms = [name.replace("_"," ").title() for name, val in dict(member.guild_permissions).items() if val]
    except Exception:
        perms = []
    embed = discord.Embed(title=f"User Info - {member}", color=discord.Color.blurple())
    embed.set_thumbnail(url=member.avatar.url if member.avatar else None)
    embed.add_field(name="Display Name", value=member.display_name, inline=True)
    embed.add_field(name="Username", value=str(member), inline=True)
    embed.add_field(name="Account Created", value=created, inline=True)
    embed.add_field(name="Joined Server", value=joined, inline=True)
    embed.add_field(name="Roles", value=", ".join(roles) or "None", inline=False)
    embed.add_field(name="Permissions", value=", ".join(perms) or "None", inline=False)
    await safe_send(ctx, embed=embed)

@bot.command(name="avatar")
async def cmd_avatar(ctx, member: discord.Member = None):
    member = member or ctx.author
    url = member.avatar.url if member.avatar else member.default_avatar.url
    embed = discord.Embed(title=f"{member.display_name}'s Avatar")
    embed.set_image(url=url)
    await safe_send(ctx, embed=embed)

# ---------------- SCHEDULED TASKS ----------------
async def schedule_unban(guild_id: int, user_id: int, unban_at: datetime):
    gid = str(guild_id)
    ensure_guild(guild_id)
    server_data[gid]["scheduled_unbans"].append({"user_id": str(user_id), "unban_iso": unban_at.isoformat()})
    save_data(server_data)
    now = datetime.utcnow()
    delay = (unban_at - now).total_seconds()
    if delay <= 0:
        delay = 1
    await asyncio.sleep(delay)
    try:
        guild = bot.get_guild(guild_id)
        if guild:
            user = await bot.fetch_user(user_id)
            await guild.unban(user)
            channel = guild.system_channel or next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
            if channel:
                await safe_send(channel, f"(＾▽＾) {user} has been unbanned automatically.")
            await log_event("moderation", f"✅ Auto-unbanned {user} in {guild.name}")
    except Exception as e:
        print(f"[schedule_unban] Error unbanning {user_id} from guild {guild_id}: {e}")
    try:
        server_data[gid]["scheduled_unbans"] = [u for u in server_data[gid]["scheduled_unbans"] if not (u["user_id"] == str(user_id) and u["unban_iso"] == unban_at.isoformat())]
        save_data(server_data)
    except Exception as e:
        print(f"[schedule_unban] Error cleaning scheduled_unbans: {e}")

async def schedule_unmute(guild_id: int, user_id: int, role_id: int, unmute_at: datetime):
    gid = str(guild_id)
    ensure_guild(guild_id)
    server_data[gid]["scheduled_unmutes"].append({"user_id": str(user_id), "role_id": role_id, "unmute_iso": unmute_at.isoformat()})
    save_data(server_data)
    now = datetime.utcnow()
    delay = (unmute_at - now).total_seconds()
    if delay <= 0:
        delay = 1
    await asyncio.sleep(delay)
    try:
        guild = bot.get_guild(guild_id)
        if guild:
            member = guild.get_member(user_id)
            role = guild.get_role(role_id)
            if member and role and role in member.roles:
                await member.remove_roles(role, reason="Temporary mute expired")
                channel = guild.system_channel or next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
                if channel:
                    await safe_send(channel, f"(｡◕‿◕｡) {member.mention} has been unmuted automatically.")
                await log_event("moderation", f"✅ Auto-unmuted {member} in {guild.name}")
    except Exception as e:
        print(f"[schedule_unmute] Error unmuting {user_id} in guild {guild_id}: {e}")
    try:
        server_data[gid]["scheduled_unmutes"] = [u for u in server_data[gid]["scheduled_unmutes"] if not (u["user_id"] == str(user_id) and u["role_id"] == role_id and u["unmute_iso"] == unmute_at.isoformat())]
        save_data(server_data)
    except Exception as e:
        print(f"[schedule_unmute] Error cleaning scheduled_unmutes: {e}")

async def resume_schedules():
    await bot.wait_until_ready()
    now = datetime.utcnow()
    for gid, data in server_data.items():
        for u in list(data.get("scheduled_unbans", [])):
            try:
                unban_time = datetime.fromisoformat(u["unban_iso"])
                user_id = int(u["user_id"])
                if unban_time > now:
                    bot.loop.create_task(schedule_unban(int(gid), user_id, unban_time))
                else:
                    bot.loop.create_task(schedule_unban(int(gid), user_id, datetime.utcnow() + timedelta(seconds=2)))
            except Exception as e:
                print(f"[resume_schedules] unban schedule error: {e}")
        for u in list(data.get("scheduled_unmutes", [])):
            try:
                unmute_time = datetime.fromisoformat(u["unmute_iso"])
                user_id = int(u["user_id"])
                role_id = int(u["role_id"])
                if unmute_time > now:
                    bot.loop.create_task(schedule_unmute(int(gid), user_id, role_id, unmute_time))
                else:
                    bot.loop.create_task(schedule_unmute(int(gid), user_id, role_id, datetime.utcnow() + timedelta(seconds=2)))
            except Exception as e:
                print(f"[resume_schedules] unmute schedule error: {e}")

# ---------------- RUN ----------------
if __name__ == "__main__":
    if not DISCORDTOKEN:
        print("ERROR: set DISCORD_BOT_TOKEN in environment/secrets.")
    else:
        if PANEL_GUILD_ID:
            ensure_guild(PANEL_GUILD_ID)
        keep_alive()
        bot.run(DISCORDTOKEN)
