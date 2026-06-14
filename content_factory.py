import os
import uuid
import datetime
import requests
import subprocess
import random
import sys
import traceback
import json
import logging
from moviepy import VideoFileClip, AudioFileClip, CompositeVideoClip, CompositeAudioClip
import yt_dlp
from groq import Groq
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Set up logging profesional
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
)
logger = logging.getLogger(__name__)


class Config:
    """Manajemen Konfigurasi dan Environment Variables."""
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    LLM_MODEL = "llama-3.3-70b-versatile"
    OUTPUT_DIR = "output"
    
    # Durasi default potong klip (detik)
    CLIP_DURATION = 30
    
    # Video quality settings
    MAX_HEIGHT = 720
    MAX_BITRATE = "2M"
    BUFFER_SIZE = "4M"

    @classmethod
    def validate(cls):
        missing = []
        if not cls.GROQ_API_KEY: missing.append("GROQ_API_KEY")
        if not cls.TELEGRAM_BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
        if not cls.TELEGRAM_CHAT_ID: missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise ValueError(f"Environment Variable berikut belum diatur: {', '.join(missing)}")


class GamingSourceDatabase:
    """Mengelola daftar link video gaming yang ingin di-clip."""
    def __init__(self, filepath="gaming_db.json"):
        self.filepath = filepath

    def get_target_video(self) -> dict:
        """Mengambil satu target video gaming untuk diproses."""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    db = json.load(f)
                    if db: return random.choice(db)
            except Exception as e:
                logger.error(f"Gagal membaca database JSON: {e}")
        
        # Fallback jika json kosong / belum dibuat
        return {
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", 
            "start_min": 1, 
            "start_sec": 15,
            "judul_game": "Gaming Montage"
        }


class TelegramNotifier:
    """Mengelola seluruh komunikasi dan log ke Telegram Bot."""
    def __init__(self):
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def send_message(self, text: str, parse_mode: str = None):
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": text}
            if parse_mode: payload["parse_mode"] = parse_mode
            requests.post(url, data=payload, timeout=10)
        except Exception as e:
            logger.error(f"Gagal mengirim pesan Telegram: {e}")

    def send_video(self, video_path: str, caption: str):
        try:
            url = f"{self.base_url}/sendVideo"
            file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
            
            # Validasi ukuran file (Telegram bot limit 50MB)
            if file_size_mb > 49:
                self.send_message(f"⚠️ *Gagal Kirim:* Ukuran video ({file_size_mb:.2f} MB) melebihi batas limit bot Telegram (50 MB).")
                return False
            
            with open(video_path, 'rb') as f:
                files = {'video': f}
                data = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "Markdown"}
                resp = requests.post(url, data=data, files=files, timeout=180)
            
            if resp.status_code == 200:
                logger.info(f"Klip Gaming sukses dikirim ke Telegram ({file_size_mb:.2f} MB).")
                return True
            else:
                logger.error(f"Telegram API error: {resp.status_code} - {resp.text}")
                self.send_message(f"❌ *Gagal kirim video:* HTTP {resp.status_code}")
                return False
                
        except requests.Timeout:
            logger.error("Telegram upload timeout (180s)")
            self.send_message("⏱ *Timeout:* Upload video melebihi 3 menit.")
            return False
        except Exception as e:
            logger.error(f"Gagal mengirim video ke Telegram: {e}")
            self.send_message(f"❌ *Error:* `{str(e)[:200]}`")
            return False


class VideoDownloaderManager:
    """Mengurus download video gaming dari YouTube secara efisien."""
    def __init__(self):
        self.temp_files = []
        os.makedirs(Config.OUTPUT_DIR, exist_ok=True)

    def track_file(self, filepath: str):
        if filepath and filepath not in self.temp_files:
            self.temp_files.append(filepath)

    def cleanup(self, keep_file: str = None):
        logger.info("Memulai pembersihan file sampah render...")
        for path in self.temp_files:
            if path == keep_file: continue
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                logger.error(f"Gagal menghapus file {path}: {e}")
        self.temp_files.clear()

    def _build_ydl_opts(self, stage: str, cookie_path: str = None):
        """Build yt-dlp options with flexible format selector."""
        # Flexible format selector - avoids HLS, prioritizes native MP4
        format_selector = (
            f'bestvideo[height<={Config.MAX_HEIGHT}][ext=mp4][protocol!=m3u8]+bestaudio[ext=m4a]/'
            f'bestvideo[height<={Config.MAX_HEIGHT}][ext=mp4][protocol!=m3u8]+bestaudio/'
            f'bestvideo[height<={Config.MAX_HEIGHT}][vcodec^=avc1][protocol!=m3u8]+bestaudio[acodec^=mp4a]/'
            f'best[height<={Config.MAX_HEIGHT}][protocol!=m3u8]/'
            f'best[height<={Config.MAX_HEIGHT}]/'
            f'best'
        )
        
        base_opts = {
            'format': format_selector,
            'quiet': True,
            'nocheckcertificate': True,
            'socket_timeout': 60,
            'retries': 3,
            'ignoreerrors': False,
            'no_warnings': True,
            'extract_flat': False,
            'noplaylist': True,
            'http_chunk_size': 10485760,
        }
        
        if cookie_path and os.path.exists(cookie_path):
            base_opts['cookiefile'] = cookie_path
            logger.info("🍪 Menggunakan cookies YouTube untuk bypass bot detection")
        
        if stage == 'search':
            base_opts.update({
                'default_search': 'ytsearch1',
                'skip_download': True,
            })
        elif stage == 'download':
            base_opts.update({})  # Will use _download_full_video + _trim_segment instead
        return base_opts

    def _download_full_video(self, url: str, cookie_path: str = None) -> str:
        """Download full video in MP4 format (no HLS) for reliable trimming."""
        temp_name = f"full_{uuid.uuid4().hex}.mp4"
        temp_path = os.path.join(Config.OUTPUT_DIR, temp_name)
        
        ydl_opts = {
            'format': (
                'bestvideo[height<=720][ext=mp4][protocol!=m3u8]+bestaudio[ext=m4a]/'
                'bestvideo[height<=720][ext=mp4][protocol!=m3u8]+bestaudio/'
                'bestvideo[height<=720][vcodec^=avc1][protocol!=m3u8]+bestaudio[acodec^=mp4a]/'
                'best[height<=720][protocol!=m3u8]/'
                'best[height<=720]/'
                'best'
            ),
            'outtmpl': temp_path,
            'quiet': True,
            'nocheckcertificate': True,
            'socket_timeout': 60,
            'retries': 3,
            'ignoreerrors': False,
            'no_warnings': True,
            'extract_flat': False,
            'noplaylist': True,
            'cookiefile': cookie_path if cookie_path and os.path.exists(cookie_path) else None,
            'http_chunk_size': 10485760,
        }
        
        logger.info(f"⬇️ Download full video (MP4, <=720p)...")
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            logger.error(f"❌ Full video download error: {e}")
            return None
            
        if not os.path.exists(temp_path) or os.path.getsize(temp_path) < 100_000:
            logger.error("❌ Full video download failed or too small")
            return None
            
        logger.info(f"✅ Full video: {temp_name} ({os.path.getsize(temp_path)/1024/1024:.2f} MB)")
        self.track_file(temp_path)
        return temp_path

    def _trim_segment(self, input_path: str, start_time_sec: float, duration: float) -> str:
        """Trim segment using local ffmpeg (fast, precise)."""
        output_name = f"segment_{uuid.uuid4().hex}.mp4"
        output_path = os.path.join(Config.OUTPUT_DIR, output_name)
        
        cmd = [
            'ffmpeg', '-y',
            '-ss', str(start_time_sec),
            '-i', input_path,
            '-t', str(duration),
            '-maxrate', Config.MAX_BITRATE,
            '-bufsize', Config.BUFFER_SIZE,
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-movflags', '+faststart',
            output_path
        ]
        
        logger.info(f"✂️ Trimming: {start_time_sec:.0f}s - {start_time_sec + duration:.0f}s")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                logger.error(f"❌ FFmpeg trim error: {result.stderr[-500:]}")
                return None
        except subprocess.TimeoutExpired:
            logger.error("❌ FFmpeg trim timeout")
            return None
        except Exception as e:
            logger.error(f"❌ Trim error: {e}")
            return None
            
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 100_000:
            logger.error("❌ Trim failed or output too small")
            return None
            
        logger.info(f"✅ Segment: {output_name} ({os.path.getsize(output_path)/1024/1024:.2f} MB)")
        self.track_file(output_path)
        return output_path

    def download_youtube_segment(self, url: str, start_time_sec: float, duration: float) -> str:
        """Download YouTube video and extract specific segment using two-phase approach."""
        # Handle search query (if not a full URL)
        if not url.startswith('http'):
            search_query = url
            url = None
        else:
            search_query = None
        
        cookie_path = "youtube_cookies.txt" if os.path.exists("youtube_cookies.txt") else None
        
        # Phase 1: Search if needed
        if search_query:
            logger.info(f"🔍 Mencari: {search_query}")
            search_opts = self._build_ydl_opts('search', cookie_path)
            try:
                with yt_dlp.YoutubeDL(search_opts) as ydl:
                    search_result = ydl.extract_info(search_query, download=False)
                
                if not search_result or "entries" not in search_result or not search_result["entries"]:
                    logger.error("❌ Tidak ada hasil pencarian")
                    return None
                
                video_info = search_result["entries"][0]
                url = video_info.get("url") or video_info.get("webpage_url")
                video_title = video_info.get("title", "Unknown")
                video_duration = video_info.get("duration", 0)
                
                logger.info(f"📺 Ditemukan: {video_title[:60]}... ({video_duration}s)")
                
                # Validate segment timing
                if start_time_sec >= video_duration:
                    start_time_sec = max(0, video_duration - duration - 5)
                    logger.warning(f"Start time melebihi durasi video, adjust ke {start_time_sec}s")
                
                if start_time_sec + duration > video_duration:
                    duration = video_duration - start_time_sec - 1
                    logger.warning(f"Durasi dipotong ke {duration}s agar muat di video")
                
                if duration <= 0:
                    logger.error("Durasi segment tidak valid")
                    return None
                    
            except Exception as e:
                logger.error(f"❌ Search/extract error: {e}")
                return None
        
        # Phase 2: Download full video (MP4, not HLS)
        full_video = self._download_full_video(url, cookie_path)
        if not full_video:
            return None
            
        # Phase 3: Trim segment locally
        segment = self._trim_segment(full_video, start_time_sec, duration)
        return segment


class AIContentEngine:
    """Mesin AI untuk membuat deskripsi/caption konten."""
    def __init__(self):
        self.client = Groq(api_key=Config.GROQ_API_KEY)

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type((requests.RequestException, Exception)),
        reraise=True
    )
    def generate_gaming_caption(self, game_name: str) -> str:
        """Membuat caption sosial media bertema gaming yang viral."""
        prompt = f"Buat caption pendek TikTok/Shorts yang seru dan relateable untuk video klip game {game_name}. Berikan hook menarik di awal, emoji gaming, dan 8 hashtag gaming viral seperti #gaming #shorts #fyp. Hanya keluarkan teks caption."
        response = self.client.chat.completions.create(
            model=Config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=200,
        )
        return response.choices[0].message.content.strip()


class VideoRenderer:
    @staticmethod
    def assemble_gaming_clip(raw_clip_path: str, game_name: str) -> str:
        """Memotong aspek rasio menjadi vertikal tanpa teks subtitle tambahan."""
        video = VideoFileClip(raw_clip_path)

        # SMART CROPPING 16:9 ke 9:16 (Fokus ke Tengah Layar / Crosshair Game)
        w, h = video.size
        target_h = 1920
        target_w = int(w * (target_h / h))
        
        video_resized = video.resized(height=target_h)
        video_vertical = video_resized.cropped(
            x_center=target_w / 2, y_center=target_h / 2, 
            width=1080, height=1920
        )

        # RENDER LANGSUNG VIDEO VERTIKAL BERSIH
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(Config.OUTPUT_DIR, f"clip_{game_name}_{timestamp}.mp4")
        
        logger.info("Memulai proses kompresi video vertikal...")
        video_vertical.write_videofile(
            output_file, codec='libx264', audio_codec='aac', fps=30, 
            preset='fast', logger=None
        )
        
        # Lepas memori berkas video
        video_vertical.close()
        video.close()
        
        return output_file


class ContentPipeline:
    """Orkestrator Utama Clipper Otomatis Bersih."""
    def __init__(self):
        Config.validate()
        self.db = GamingSourceDatabase()
        self.notifier = TelegramNotifier()
        self.asset_mgr = VideoDownloaderManager()
        self.ai_engine = AIContentEngine()

    def run(self):
        video_final = None
        try:
            logger.info("🚀 Memulai Gaming Clipper Factory (Clean Mode)...")
            self.notifier.send_message("🚀 Sedang memotong klip gaming terbaru...")

            # 1. Ambil target video (now uses search_query + judul_game)
            target = self.db.get_target_video()
            search_query = target["search_query"]
            game_title = target["judul_game"]
            
            # Random start time for variety (60-300 seconds into video)
            start_seconds = random.uniform(60, 300)
            
            # 2. Download potongan video mentah dari YouTube (supports search query)
            raw_clip = self.asset_mgr.download_youtube_segment(
                search_query, start_seconds, Config.CLIP_DURATION
            )
            
            if not raw_clip:
                raise ValueError("Gagal mengunduh potongan video dari YouTube.")

            # 3. Merakit video (Crop ke vertikal secara bersih)
            video_final = VideoRenderer.assemble_gaming_clip(raw_clip, game_title)

            # 4. Buat Caption dengan Groq LLM
            caption = self.ai_engine.generate_gaming_caption(game_title)

            # 5. Kirim ke Telegram
            telegram_caption = (
                f"🎮 *KLIP GAMING OTOMATIS SIAP UPLOAD*\n\n"
                f"🎮 *Game:* {game_title}\n"
                f"🔍 *Query:* {search_query}\n\n"
                f"📝 *Caption Konten:*\n`{caption}`"
            )
            self.notifier.send_video(video_final, telegram_caption)
            self.notifier.send_message("✅ Sukses memproses klip gaming!")

        except Exception as e:
            error_details = traceback.format_exc()
            logger.critical(f"Sistem Crash: {e}\n{error_details}")
            self.notifier.send_message(f"❌ *Clipper Gagal!* Terjadi error:\n`{str(e)}`")
            sys.exit(1)
            
        finally:
            self.asset_mgr.cleanup(keep_file=video_final)


if __name__ == "__main__":
    factory = ContentPipeline()
    factory.run()