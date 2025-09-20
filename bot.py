import os
import re
import asyncio
import uuid
import time
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Import configuration - try to import from config.py or use environment variable
try:
    from config import BOT_TOKEN
except ImportError:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "8321463095:AAG1gHVQ_nO3sllANJB67ufB0gBoiVDvgTc")

# Configuration
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/sdcard/Download/TwitterSpaces"))
PROGRESS_UPDATE_COOLDOWN = 30

# Create download directory
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------
# START command
# ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Send me a Twitter Space link (from x.com or twitter.com) and I'll download the audio for you.\n\n"
        "⚠️ If the download is interrupted, you'll receive whatever was downloaded so far."
    )

# ---------------------------
# Async extract stream url
# ---------------------------
async def extract_stream_url(space_url: str) -> str | None:
    if "x.com" in space_url:
        space_url = space_url.replace("x.com", "twitter.com")

    cmd = ["yt-dlp", "-g", "-f", "bestaudio", space_url]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return None
        text = stdout.decode().strip()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("http"):
                return line
        return None
    except Exception:
        return None

# ---------------------------
# Async ffmpeg runner with progress
# ---------------------------
async def run_ffmpeg_with_progress(m3u8_url: str, output_path: Path, progress_callback):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    cmd = [
        "ffmpeg",
        "-y",
        "-i", m3u8_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-progress", "pipe:1",
        "-nostats",
        str(output_path)
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    combined = []
    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode(errors="ignore").strip()
            combined.append(line)
            if "=" in line:
                k, v = line.split("=", 1)
                await progress_callback(k, v)
        await proc.wait()
        return proc.returncode, "\n".join(combined)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        raise

# ---------------------------
# Fallback conversion
# ---------------------------
async def run_ffmpeg_fallback(m3u8_url: str, output_path: Path, progress_callback):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    cmd = [
        "ffmpeg",
        "-y",
        "-i", m3u8_url,
        "-c:a", "libmp3lame",
        "-b:a", "128k",
        "-progress", "pipe:1",
        "-nostats",
        str(output_path)
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    combined = []
    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode(errors="ignore").strip()
            combined.append(line)
            if "=" in line:
                k, v = line.split("=", 1)
                await progress_callback(k, v)
        await proc.wait()
        return proc.returncode, "\n".join(combined)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        raise

# ---------------------------
# Helper: sanitize filename
# ---------------------------
def make_unique_filename_from_url(url: str, ext: str = ".mp3") -> str:
    base = re.sub(r'\W+', '_', url)[:50]
    ts = int(time.time())
    uid = uuid.uuid4().hex[:8]
    return f"{base}_{ts}_{uid}{ext}"

# ---------------------------
# Send available audio to user
# ---------------------------
async def send_available_audio(msg, file_path):
    """Send whatever audio is available to the user"""
    try:
        if file_path.exists():
            file_size = file_path.stat().st_size
            if file_size > 1024:  # At least 1KB
                try:
                    await msg.reply_audio(audio=open(file_path, "rb"))
                    await msg.reply_text("📦 Sent partially downloaded audio.")
                    return True
                except Exception as e:
                    await msg.reply_text(f"📦 Partial file available at: {file_path} (Size: {file_size//1024}KB)")
                    return True
    except Exception as e:
        print(f"Error accessing partial file: {e}")
    
    return False

# ---------------------------
# Worker: download & send audio
# ---------------------------
async def download_and_send(msg, m3u8_url, output_path):
    last_progress_time = 0
    last_minutes_reported = -1

    async def progress_callback(key, value):
        nonlocal last_progress_time, last_minutes_reported
        now = time.time()
        try:
            if key == "out_time_ms":
                ms = int(value)
                seconds = ms // 1000000
                minutes = seconds // 60
                if now - last_progress_time >= PROGRESS_UPDATE_COOLDOWN and minutes != last_minutes_reported:
                    last_progress_time = now
                    last_minutes_reported = minutes
                    await msg.reply_text(f"⏳ Downloaded ~{minutes} minutes so far...")
            elif key == "progress" and value.strip().lower() == "end":
                await msg.reply_text("✅ Processing complete.")
        except Exception:
            return

    try:
        returncode, output = await run_ffmpeg_with_progress(str(m3u8_url), output_path, progress_callback)
        
        if returncode != 0:
            await msg.reply_text("⚠️ Fast method failed, trying alternative approach...")
            returncode, output = await run_ffmpeg_fallback(str(m3u8_url), output_path, progress_callback)
        
        if returncode != 0 or not output_path.exists():
            await msg.reply_text("⚠️ Failed to process audio.")
            # Try to send whatever we have
            await send_available_audio(msg, output_path)
            return

        file_size = output_path.stat().st_size
        if file_size < 1024:
            await msg.reply_text("⚠️ Downloaded file is too small.")
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass
            return

        try:
            # Add debug info
            await msg.reply_text(f"📊 File size: {file_size//1024}KB, attempting to send...")
            await msg.reply_audio(audio=open(output_path, "rb"))
            await msg.reply_text("✅ Download complete! File sent.")
        except Exception as e:
            await msg.reply_text(f"⚠️ Failed to send file: {e}. The file was saved to `{output_path}`.")
        finally:
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass
                
    except asyncio.CancelledError:
        # Download was cancelled, send what we have
        try:
            await msg.reply_text("⚠️ Download was interrupted.")
            await send_available_audio(msg, output_path)
        except Exception:
            # If we can't send a message, at least try to save the file
            if output_path.exists() and output_path.stat().st_size > 1024:
                print(f"Download interrupted. File available at: {output_path}")
        raise
        
    except Exception as e:
        try:
            await msg.reply_text(f"⚠️ Error while processing: {e}")
            # Try to send whatever we have
            await send_available_audio(msg, output_path)
        except Exception:
            # If we can't send a message, at least try to save the file
            if output_path.exists() and output_path.stat().st_size > 1024:
                print(f"Error during processing. File available at: {output_path}")
        
    finally:
        # Clean up if file is too small
        try:
            if output_path.exists() and output_path.stat().st_size < 1024:
                output_path.unlink(missing_ok=True)
        except Exception:
            pass

# ---------------------------
# Handler for incoming messages
# ---------------------------
async def handle_space(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    url = msg.text.strip()

    if not ("twitter.com/i/spaces" in url or "x.com/i/spaces" in url):
        await msg.reply_text("⚠️ Please send a valid Twitter Space link.")
        return

    await msg.reply_text("⏳ Extracting audio stream...")

    m3u8_url = await extract_stream_url(url)
    if not m3u8_url:
        await msg.reply_text("⚠️ Failed to extract audio stream. The link might be private or unsupported.")
        return

    user_id = str(update.effective_user.id if update.effective_user else "anonymous")
    user_dir = DOWNLOAD_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)

    fname = make_unique_filename_from_url(url, ext=".m4a")
    output_path = user_dir / fname

    await msg.reply_text("🎶 Downloading audio (this can take a while). I will update you periodically...")

    # Create task
    task = asyncio.create_task(download_and_send(msg, m3u8_url, output_path))
    
    # Store task reference to potentially cancel it later
    if not hasattr(context, 'user_data'):
        context.user_data = {}
    context.user_data['download_task'] = task

# ---------------------------
# Cancel command to stop download
# ---------------------------
async def cancel_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if hasattr(context, 'user_data') and 'download_task' in context.user_data:
        task = context.user_data['download_task']
        if not task.done():
            task.cancel()
            await msg.reply_text("⏹️ Download cancelled. Sending whatever was downloaded so far...")
        else:
            await msg.reply_text("No active download to cancel.")
    else:
        await msg.reply_text("No active download to cancel.")

# ---------------------------
# MAIN
# ---------------------------
def main():
    # Check if bot token is set
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: Please set your bot token in config.py or as BOT_TOKEN environment variable")
        print("You can get your token from @BotFather on Telegram")
        return
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel_download))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_space))
    
    print("Bot is starting...")
    print("Use /cancel to stop a download and receive partial files.")
    
    app.run_polling(allowed_updates=None)

if __name__ == "__main__":
    main()
