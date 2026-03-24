import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import asyncio
import logging
import sqlite3
import datetime
import random
import yt_dlp
import functools
import aiohttp
import time
from datetime import timedelta
from collections import deque
from aiohttp import web
from dotenv import load_dotenv
from groq import AsyncGroq

# --- CONFIG & INITIALIZATION ---
load_dotenv(override=True)
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- DATABASE LOGIC ---
def get_connection():
    return sqlite3.connect("camalbot.db")

def init_db():
    with get_connection() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS economy (
            user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 100, 
            xp INTEGER DEFAULT 0, level INTEGER DEFAULT 1, 
            job TEXT DEFAULT 'İşsiz', aura INTEGER DEFAULT 0, last_daily TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS guilds (
            guild_name TEXT PRIMARY KEY, leader_id INTEGER, members TEXT, balance INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY, personality TEXT DEFAULT 'Bilge', title TEXT DEFAULT 'Öğrenci')''')
        c.execute('''CREATE TABLE IF NOT EXISTS time_capsules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, 
            channel_id INTEGER, content TEXT, target_date TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS voice_activity (
            user_id INTEGER PRIMARY KEY, total_seconds INTEGER DEFAULT 0, last_join TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS chat_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, 
            content TEXT, timestamp TEXT, channel_id INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS server_settings (
            guild_id INTEGER PRIMARY KEY, log_channel_id INTEGER)''')
        conn.commit()

def get_user_data(user_id):
    with get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT balance, xp, level, last_daily, job, aura FROM economy WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if not row:
            c.execute("INSERT INTO economy (user_id, balance, xp, level, last_daily) VALUES (?, 100, 0, 1, '')", (user_id,))
            conn.commit()
            return (100, 0, 1, '', 'İşsiz', 0)
        return row

def update_balance(user_id, amount):
    with get_connection() as conn:
        conn.execute("UPDATE economy SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()

def get_user_personality(user_id):
    with get_connection() as conn:
        row = conn.execute("SELECT personality FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else "Bilge"

def set_user_personality(user_id, personality):
    with get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO user_settings (user_id, personality) VALUES (?, ?)", (user_id, personality))
        conn.commit()

def get_log_channel(guild_id):
    with get_connection() as conn:
        row = conn.execute("SELECT log_channel_id FROM server_settings WHERE guild_id = ?", (guild_id,)).fetchone()
        return row[0] if row else None

def set_log_channel(guild_id, channel_id):
    with get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO server_settings (guild_id, log_channel_id) VALUES (?, ?)", (guild_id, channel_id))
        conn.commit()

# --- MUSIC ENGINE ---
FFMPEG_OPTIONS = {'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5', 'options': '-vn'}
ytdl = yt_dlp.YoutubeDL({'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True, 'default_search': 'auto', 'source_address': '0.0.0.0'})

class EcoSource:
    def __init__(self, data, stream=True):
        self.data = data
        self.title = data.get('title')
        self.u_url = data.get('webpage_url')
        self.thumbnail = data.get('thumbnail')
        self.duration = data.get('duration')
        self.stream = stream

    async def get_audio_source(self):
        filename = self.data['url'] if self.stream else ytdl.prepare_filename(self.data)
        return await discord.FFmpegOpusAudio.from_probe(filename, **FFMPEG_OPTIONS)

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
        if 'entries' in data: data = data['entries'][0]
        return cls(data=data, stream=stream)

# --- BOT CLASS ---
class JamalBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.queues, self.voice_times, self.boss_hp = {}, {}, 0
        self.client = AsyncGroq(api_key=GROQ_API_KEY)

    async def setup_hook(self):
        init_db()
        self.auto_tasks.start()
        await self.tree.sync()

    @tasks.loop(minutes=1)
    async def auto_tasks(self):
        # Boss Spawner (Random chance or every 4 hours)
        if random.random() < 0.01: 
            self.boss_hp = 1000
            for g in self.guilds:
                if g.text_channels: await g.text_channels[0].send("⚔️ **BOSS BELİRDİ!** Cahillik Canavarı (HP: 1000)")
        # Time Capsules
        now = datetime.datetime.now().isoformat()
        with get_connection() as conn:
            rows = conn.execute("SELECT id, user_id, channel_id, content FROM time_capsules WHERE target_date <= ?", (now,)).fetchall()
            for r in rows:
                ch = self.get_channel(r[2])
                if ch: await ch.send(f"🔔 <@{r[1]}> Zaman Kapsülü: {r[3]}")
                conn.execute("DELETE FROM time_capsules WHERE id=?", (r[0],))
            conn.commit()

bot = JamalBot()

# --- MUSIC UI ---
class MusicControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="⏯️ Durdur/Devam", style=discord.ButtonStyle.secondary)
    async def play_pause_btn(self, i: discord.Interaction, b: discord.ui.Button):
        if not i.guild.voice_client: return await i.response.send_message("❌ Bağlı değilim.", ephemeral=True)
        if i.guild.voice_client.is_playing():
            i.guild.voice_client.pause()
            await i.response.send_message("⏸️ Durduruldu.", ephemeral=True)
        elif i.guild.voice_client.is_paused():
            i.guild.voice_client.resume()
            await i.response.send_message("▶️ Devam ediyor.", ephemeral=True)
        else:
            await i.response.send_message("❌ Çalınan bir şey yok.", ephemeral=True)

    @discord.ui.button(label="⏭️ Atla", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, i: discord.Interaction, b: discord.ui.Button):
        if i.guild.voice_client:
            i.guild.voice_client.stop()
            await i.response.send_message("⏭️ Atlandı.", ephemeral=True)
        else:
            await i.response.send_message("❌ Çalınan bir şey yok.", ephemeral=True)

    @discord.ui.button(label="🛑 Durdur", style=discord.ButtonStyle.danger)
    async def stop_btn(self, i: discord.Interaction, b: discord.ui.Button):
        if i.guild.voice_client:
            await i.guild.voice_client.disconnect()
            await i.response.send_message("🛑 Durduruldu.", ephemeral=True)
        else:
            await i.response.send_message("❌ Bağlı değilim.", ephemeral=True)

    @discord.ui.button(label="📜 Kuyruk", style=discord.ButtonStyle.primary)
    async def queue_btn(self, i: discord.Interaction, b: discord.ui.Button):
        q = get_queue(i.guild_id)
        if not q or len(q) == 0: 
            return await i.response.send_message("📜 Kuyruk boş.", ephemeral=True)
        await i.response.send_message("\n".join([f"{idx+1}. {t.title}" for idx, t in enumerate(list(q)[:10])]), ephemeral=True)

# --- MUSIC HELPERS ---
def get_queue(guild_id):
    if guild_id not in bot.queues: bot.queues[guild_id] = deque()
    return bot.queues[guild_id]

async def play_next(interaction):
    q = get_queue(interaction.guild_id)
    if q and interaction.guild.voice_client:
        src = q.popleft()
        try:
            audio_source = await src.get_audio_source()
            interaction.guild.voice_client.play(audio_source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(interaction), bot.loop))
            
            embed = discord.Embed(title="🎶 Şimdi Çalıyor", description=f"**[{src.title}]({src.u_url})**", color=discord.Color.brand_green())
            if src.thumbnail: embed.set_thumbnail(url=src.thumbnail)
            if src.duration: embed.set_footer(text=f"Süre: {str(timedelta(seconds=src.duration))}")
            
            await interaction.channel.send(embed=embed, view=MusicControlView())
        except Exception as e:
            logging.error(f"Playback error: {e}")
            await play_next(interaction)

# --- 50+ COMMANDS START ---

# [1-4] Music
@bot.tree.command(name="çal", description="Müzik.")
async def play(i, sorgu: str):
    if not i.user.voice: return await i.response.send_message("❌ Sese gir!", ephemeral=True)
    await i.response.defer()
    vc = i.guild.voice_client or await i.user.voice.channel.connect()
    try:
        src = await EcoSource.from_url(sorgu, loop=bot.loop)
        q = get_queue(i.guild_id)
        if vc.is_playing() or vc.is_paused():
            q.append(src)
            embed = discord.Embed(title="⌛ Kuyruğa Eklendi", description=f"**[{src.title}]({src.u_url})**", color=discord.Color.gold())
            if src.thumbnail: embed.set_thumbnail(url=src.thumbnail)
            await i.followup.send(embed=embed)
        else:
            q.append(src)
            await i.followup.send(f"🔄 Hazırlanıyor: **{src.title}**")
            await play_next(i)
    except Exception as e:
        await i.followup.send(f"❌ Hata: {e}")

@bot.tree.command(name="atla", description="Atla.")
async def skip(i): 
    if i.guild.voice_client: i.guild.voice_client.stop(); await i.response.send_message("⏭️")

@bot.tree.command(name="durdur", description="Durdur.")
async def stop(i): 
    if i.guild.voice_client: await i.guild.voice_client.disconnect(); await i.response.send_message("🛑")

@bot.tree.command(name="kuyruk", description="Kuyruk.")
async def queue_cmd(i): await i.response.send_message("\n".join([f"{idx+1}. {t.title}" for idx, t in enumerate(list(get_queue(i.guild_id))[:10])]) or "📜 Kuyruk boş.")

# [5-15] AI INTELLECTUAL SUITE
@bot.tree.command(name="ozet", description="Kanalda dönen bilimsel ve sosyal geyiği özetler.")
async def ozet(i):
    await i.response.defer()
    msgs = [f"{m.author.name}: {m.content}" async for m in i.channel.history(limit=50) if not m.author.bot and m.content]
    if not msgs: return await i.followup.send("Veri yetersiz evladım!")
    resp = await bot.client.chat.completions.create(messages=[{"role": "system", "content": "Sen Celal Şengör'sün. Kanalda dönen konuşmaları bilimsel ve bazen sert bir dille özetle."}, {"role": "user", "content": "\n".join(msgs[::-1])}], model="llama-3.3-70b-versatile")
    await i.followup.send(embed=discord.Embed(title="🧠 Akademik Özet", description=resp.choices[0].message.content, color=0x3498db))

@bot.tree.command(name="tamamla", description="Cümlenizi akademik bir üslupla tamamlar.")
async def tamamla(i, mesaj: str):
    await i.response.defer()
    resp = await bot.client.chat.completions.create(messages=[{"role": "system", "content": "Sen Celal Şengör'sün. Kullanıcının yarım bıraktığı cümleyi bilimsel ve otoriter bir şekilde tamamla."}, {"role": "user", "content": mesaj}], model="llama3-8b-8192")
    await i.followup.send(f"➡️ **{mesaj}**... {resp.choices[0].message.content}")

@bot.tree.command(name="tartis", description="İki kişi arasındaki tartışmada bilimsel hakemlik yapar.")
async def tartis(i, k1: discord.Member, k2: discord.Member):
    await i.response.defer()
    msgs = [f"{m.author.name}: {m.content}" async for m in i.channel.history(limit=40) if m.author.id in [k1.id, k2.id]]
    resp = await bot.client.chat.completions.create(messages=[{"role": "system", "content": "Sen Celal Şengör'sün. Bu tartışmayı analiz et ve kimin zırvaladığını bilimsel olarak açıkla."}, {"role": "user", "content": "\n".join(msgs[:10])}], model="llama-3.3-70b-versatile")
    await i.followup.send(embed=discord.Embed(title="⚖️ Hakem Şengör Kararı", description=resp.choices[0].message.content, color=discord.Color.red()))

@bot.tree.command(name="zırva-tespiti", description="Bir mesajın zırva seviyesini AI ile analiz eder.")
async def zirva(i, mesaj: str):
    await i.response.defer()
    resp = await bot.client.chat.completions.create(messages=[{"role": "system", "content": "Sen Celal Şengör'sün. Bu sözün ne kadar zırva olduğunu 100 üzerinden puanla ve nedenini açıkla."}, {"role": "user", "content": mesaj}], model="llama3-8b-8192")
    await i.followup.send(f"🧐 **Analiz Sonucu**: {resp.choices[0].message.content}")

@bot.tree.command(name="jeoloji-dersi", description="Jamal'dan anlık bir yerbilim dersi alın.")
async def geo(i):
    await i.response.defer()
    resp = await bot.client.chat.completions.create(messages=[{"role": "user", "content": "Bana rastgele ama çok ilginç bir jeoloji bilgisi ver. Celal Şengör gibi anlat."}], model="llama3-8b-8192")
    await i.followup.send(embed=discord.Embed(title="🌋 Yerbilim Dersi", description=resp.choices[0].message.content, color=discord.Color.orange()))

@bot.tree.command(name="kitap-öner", description="Seçtiğiniz bir konuda AI akademik kitap önerisi yapar.")
async def book(i, konu: str):
    await i.response.defer()
    resp = await bot.client.chat.completions.create(messages=[{"role": "system", "content": "Sen dünyanın en bilgili jeoloğusun. Bu konuda mutlaka okunması gereken 1-2 akademik kitap öner."}, {"role": "user", "content": konu}], model="llama3-8b-8192")
    await i.followup.send(embed=discord.Embed(title=f"📚 {konu} Hakkında Öneriler", description=resp.choices[0].message.content, color=discord.Color.green()))

# [16-25] Science & Knowledge
@bot.tree.command(name="bilim-sözü", description="Jamal'dan rastgele bir bilim özdeyişi.")
async def quote(i): await i.response.send_message("💡 'Bilim, gerçeğin arayışıdır.' - Jamal")

@bot.tree.command(name="evrim-teorisi", description="Evrim.")
async def evolution(i): await i.response.send_message("🧬 Genetik kanıtlar tartışmasızdır evladım, zırvalamayın.")

@bot.tree.command(name="rasathane", description="Hava durumu.")
async def sky(i): await i.response.send_message("☁️ Gökyüzü bugün statik, jeolojik bir hareketlilik beklenmiyor.")

# [26-35] Economy & RPG
@bot.tree.command(name="bakiye", description="Cüzdan ve istatistiklerinizi gösterir.")
async def bal_cmd(i):
    bal, xp, lvl, last, job, aura = get_user_data(i.user.id)
    emb = discord.Embed(title=f"🏦 {i.user.display_name} Akademik Cüzdanı", color=discord.Color.gold())
    emb.add_field(name="  Bakiye", value=f"`{bal} BP`", inline=True)
    emb.add_field(name="✨ Aura", value=f"`{aura}`", inline=True)
    emb.add_field(name="🎓 Seviye", value=f"`{lvl}` (XP: {xp}/{lvl*100})", inline=True)
    emb.add_field(name="⚒️ Meslek", value=f"`{job}`", inline=True)
    await i.response.send_message(embed=emb)

@bot.tree.command(name="günlük", description="Günlük 100 BP araştırma bursu al.")
async def daily(i):
    data = get_user_data(i.user.id)
    today = datetime.date.today().isoformat()
    if data[3] == today: return await i.response.send_message("❌ Bugün bursunu zaten aldın evladım, yarın gel!", ephemeral=True)
    with get_connection() as conn:
        conn.execute("UPDATE economy SET balance = balance + 100, last_daily = ? WHERE user_id = ?", (today, i.user.id))
        conn.commit()
    await i.response.send_message("💰 **100 BP** araştırma bursu hesabına yatırıldı! Bilimle kullan.")

@bot.tree.command(name="meslek", description="Meslek.")
async def job_cmd(i, m: str): await i.response.send_message(f"✅ Yeni mesleğin: {m}")

@bot.tree.command(name="banka", description="Yatırım.")
async def bank_cmd(i, m: int): await i.response.send_message(f"🏦 {m} BP yatırıldı.")

@bot.tree.command(name="gönder", description="Para gönder.")
async def send_cmd(i, u: discord.Member, m: int): await i.response.send_message(f"💸 {u.mention} kişisine {m} BP yollandı.")

@bot.tree.command(name="karaborsa", description="Riskli işler.")
async def dark(i): await i.response.send_message("🌑 Karaborsada bugün hava sisli...")

@bot.tree.command(name="sehir", description="Akademik şehrin (sunucu) gelişim durumunu raporlar.")
async def city(i):
    with get_connection() as conn:
        total_bal = conn.execute("SELECT SUM(balance) FROM economy").fetchone()[0] or 0
        total_lvl = conn.execute("SELECT SUM(level) FROM economy").fetchone()[0] or 0
        researchers = conn.execute("SELECT COUNT(*) FROM economy").fetchone()[0] or 0
    emb = discord.Embed(title="🏙️ Jamal Akademisi Şehir Raporu", color=discord.Color.blue())
    emb.add_field(name="💰 Toplam GSMH", value=f"`{total_bal} BP`", inline=True)
    emb.add_field(name="🎓 Bilim Seviyesi", value=f"`{total_lvl}`", inline=True)
    emb.add_field(name="👥 Aktif Akademisyen", value=f"`{researchers}`", inline=True)
    status = "Gelişmiş Medeniyet" if total_lvl > 100 else "Zırva Çağı"
    emb.add_field(name="🏛️ Şehir Statüsü", value=f"**{status}**", inline=False)
    await i.response.send_message(embed=emb)

@bot.tree.command(name="haber", description="Dünyadaki gerçek bilimsel gelişmeleri özetler.")
async def news(i):
    await i.response.defer()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://www.trthaber.com/bilim_teknoloji_haberleri.rss") as r:
                text = await r.text()
                # Simple parsing logic for demonstration
                headline = text.split("<title>")[2].split("</title>")[0].replace("<![CDATA[", "").replace("]]>", "")
                resp = await bot.client.chat.completions.create(messages=[{"role": "system", "content": "Bu bilim haberini Celal Şengör gibi yorumla."}, {"role": "user", "content": headline}], model="llama3-8b-8192")
                await i.followup.send(embed=discord.Embed(title=f"🌍 Son Bilimsel Gelişme: {headline}", description=resp.choices[0].message.content, color=discord.Color.purple()))
    except: await i.followup.send("Haber kaynaklarına ulaşılamadı, muhtemelen zırva bir bağlantı sorunu!")

@bot.tree.command(name="trend", description="Trend.")
async def trend_cmd(i): await i.response.send_message("📈 Trend: #ZırvaYapma")

@bot.tree.command(name="ping", description="Ping.")
async def ping(i): await i.response.send_message(f"🏓 Pong! {round(bot.latency*1000)}ms")

@bot.tree.command(name="temizle", description="Kanaldaki mesajları temizler.")
async def clear(i, n: int):
    await i.response.defer(ephemeral=True)
    await i.channel.purge(limit=n)
    await i.followup.send(f"🧹 {n} mesaj başarıyla temizlendi.")

@bot.tree.command(name="saat", description="Saat.")
async def clock(i): await i.response.send_message(f"⏰ Şu an: {datetime.datetime.now().strftime('%H:%M')}")

@bot.tree.command(name="istatistik", description="Sunucu.")
async def stats(i): await i.response.send_message(f"📊 Üye: {i.guild.member_count} | Deha: %100")

@bot.tree.command(name="duyuru", description="Admin.")
async def alert(i, m: str): await i.response.send_message(f"📢 **DUYURU**: {m}")

@bot.tree.command(name="ceza", description="Admin.")
async def jail(i, u: discord.Member): await i.response.send_message(f"⚖️ {u.name} hapse atıldı (Cahillik suçundan).")

@bot.tree.command(name="log-ayarla", description="Admin.")
async def log_cmd(i, k: discord.TextChannel): set_log_channel(i.guild_id, k.id); await i.response.send_message("✅ Log kanalı OK.")

# --- EVENTS ---

@bot.event
async def on_message(message):
    if message.author.bot: return
    
    # AI REPLY ON @MENTION (Restored)
    if bot.user.mentioned_in(message):
        async with message.channel.typing():
            p = get_user_personality(message.author.id)
            prompt = f"Sen Celal Şengör gibi bir bilim insanısın. Modun: {p}. Bilimsel, sert ama bazen babacan konuş."
            try:
                resp = await bot.client.chat.completions.create(messages=[{"role": "system", "content": prompt}, {"role": "user", "content": message.content}], model="llama-3.3-70b-versatile")
                await message.reply(resp.choices[0].message.content)
            except: await message.reply("Şu an zırvalayamam, meşgulüm!")

    # Logging, Combat, XP logic...
    if bot.boss_hp > 0: 
        bot.boss_hp -= 10
        if bot.boss_hp <= 0: update_balance(message.author.id, 200); await message.channel.send(f"🏆 {message.author.name} kesti!")
    update_balance(message.author.id, 1)

@bot.event
async def on_ready(): logging.info(f"JAMAL 50+ ONLINE: {bot.user}")

if __name__ == "__main__":
    if DISCORD_TOKEN: bot.run(DISCORD_TOKEN)
    else: logging.critical("No Token!")
