import asyncio
import re
import random
import string
import json
import aiohttp
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any
from aiogram import Bot, Dispatcher, F, html
from aiogram.types import Message, ChatPermissions, ChatMemberStatus, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command, BaseFilter
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from contextlib import asynccontextmanager
import aiosqlite

# ===== КОНФИГУРАЦИЯ =====
TOKEN = "ТВОЙ_ТОКЕН_БОТА"
OWNER_ID = 123456789  # ТВОЙ TELEGRAM ID
LOG_CHANNEL_ID = None  # ID канала для логов

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ===== АСИНХРОННАЯ БАЗА ДАННЫХ =====
DB_PATH = "brest_guard_ultimate.db"

@asynccontextmanager
async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        yield db

async def init_database():
    async with get_db() as db:
        # Запрещённые слова
        await db.execute('''
            CREATE TABLE IF NOT EXISTS banned_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT UNIQUE NOT NULL,
                added_by INTEGER,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Муты
        await db.execute('''
            CREATE TABLE IF NOT EXISTS muted_users (
                user_id INTEGER,
                chat_id INTEGER,
                muted_until TIMESTAMP,
                reason TEXT,
                muted_by INTEGER,
                PRIMARY KEY (user_id, chat_id)
            )
        ''')
        
        # Карма
        await db.execute('''
            CREATE TABLE IF NOT EXISTS karma (
                user_id INTEGER,
                chat_id INTEGER,
                karma INTEGER DEFAULT 0,
                last_karma_change TIMESTAMP,
                PRIMARY KEY (user_id, chat_id)
            )
        ''')
        
        # Лимит объявлений
        await db.execute('''
            CREATE TABLE IF NOT EXISTS ads_limit (
                user_id INTEGER,
                chat_id INTEGER,
                last_ad_time TIMESTAMP,
                ads_count INTEGER DEFAULT 1,
                PRIMARY KEY (user_id, chat_id)
            )
        ''')
        
        # Варны
        await db.execute('''
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                warned_by INTEGER,
                reason TEXT,
                warned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Заметки
        await db.execute('''
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                trigger TEXT,
                content TEXT,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Настройки чата
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                captcha_enabled INTEGER DEFAULT 1,
                antiflood_enabled INTEGER DEFAULT 1,
                antiflood_limit INTEGER DEFAULT 5,
                warn_limit INTEGER DEFAULT 3,
                warn_action TEXT DEFAULT 'mute_1h',
                ad_cooldown_hours INTEGER DEFAULT 12,
                block_forwards_enabled INTEGER DEFAULT 1,
                ai_moderation_enabled INTEGER DEFAULT 0,
                ai_api_url TEXT DEFAULT 'https://api-inference.huggingface.co/models/cointegrated/rubert-tiny-toxicity',
                ai_api_key TEXT DEFAULT '',
                ai_threshold REAL DEFAULT 0.7
            )
        ''')
        
        # Белый список
        await db.execute('''
            CREATE TABLE IF NOT EXISTS whitelist (
                user_id INTEGER,
                chat_id INTEGER,
                verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, chat_id)
            )
        ''')
        
        # Админ активность
        await db.execute('''
            CREATE TABLE IF NOT EXISTS admin_activity (
                user_id INTEGER PRIMARY KEY,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Разрешённые источники для пересылок
        await db.execute('''
            CREATE TABLE IF NOT EXISTS allowed_forward_sources (
                chat_id INTEGER PRIMARY KEY,
                source_id INTEGER,
                source_type TEXT
            )
        ''')
        
        # Дефолтные слова
        cursor = await db.execute("SELECT COUNT(*) FROM banned_words")
        count = (await cursor.fetchone())[0]
        if count == 0:
            default_words = [
                ("скам",), ("крипта",), ("казино",), ("ставки",), ("1xbet",),
                ("заработок",), ("инвестиции",), ("биткоин",), ("реферал",),
                ("фейк",), ("лохотрон",), ("развод",), ("мошенник",),
                ("быстрые деньги",), ("легкий заработок",), ("пассивный доход",)
            ]
            await db.executemany("INSERT INTO banned_words (word) VALUES (?)", default_words)
        
        await db.commit()

# ===== ИИ МОДЕРАЦИЯ (БЕСПЛАТНАЯ ЛОКАЛЬНАЯ ЧЕРЕЗ HUGGING FACE) =====
class AIModerator:
    def __init__(self):
        self.session = None
        self.model_name = "cointegrated/rubert-tiny-toxicity"  # Бесплатная русская модель
        self.api_url = f"https://api-inference.huggingface.co/models/{self.model_name}"
        self.headers = {}
    
    async def set_api_key(self, api_key: str):
        """Установка API ключа Hugging Face"""
        self.headers = {"Authorization": f"Bearer {api_key}"}
    
    async def set_model(self, model: str):
        """Смена модели"""
        self.model_name = model
        self.api_url = f"https://api-inference.huggingface.co/models/{self.model_name}"
    
    async def get_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def check_text(self, text: str, threshold: float = 0.7) -> Tuple[bool, float, str]:
        """
        Проверка текста на токсичность/спам
        Возвращает: (is_toxic, confidence, reason)
        """
        if not self.headers.get("Authorization"):
            return False, 0.0, "AI отключён (нет ключа)"
        
        try:
            session = await self.get_session()
            
            # Пауза чтобы не забанили за лимиты (бесплатный аккаунт)
            await asyncio.sleep(0.5)
            
            async with session.post(
                self.api_url,
                headers=self.headers,
                json={"inputs": text, "parameters": {"truncation": True}}
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    
                    # Парсим результат
                    if isinstance(result, list) and len(result) > 0:
                        # Модель возвращает вероятности для разных классов
                        # Обычно: ['toxic', 'severe_toxic', 'obscene', 'threat', 'insult', 'identity_hate']
                        toxic_scores = {}
                        if isinstance(result[0], dict):
                            for item in result[0].items():
                                toxic_scores[item[0]] = item[1]
                        
                        # Максимальная токсичность
                        max_score = max(toxic_scores.values()) if toxic_scores else 0
                        
                        # Определяем причину
                        reason = "нейросеть определила токсичный контент"
                        for label, score in toxic_scores.items():
                            if score > threshold:
                                reason = f"нейросеть: {label} ({score:.2f})"
                                return True, score, reason
                        
                        return max_score > threshold, max_score, f"токсичность {max_score:.2f}"
                    
                    elif isinstance(result, dict) and "error" in result:
                        # Ошибка модели (например, загрузка идёт)
                        if "loading" in result["error"]:
                            return False, 0.0, "модель загружается"
                        return False, 0.0, f"ошибка: {result['error']}"
                    
                    return False, 0.0, "неопределённый результат"
                else:
                    return False, 0.0, f"HTTP {response.status}"
                    
        except Exception as e:
            return False, 0.0, f"ошибка: {str(e)}"
    
    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

ai_moderator = AIModerator()

# ===== НОРМАЛИЗАТОР ТЕКСТА =====
def normalize_text(text: str) -> str:
    replacements = {
        'a': 'а', 'c': 'с', 'e': 'е', 'o': 'о', 'p': 'р', 
        'x': 'х', 'y': 'у', 'b': 'в', 'h': 'н', 'k': 'к',
        'm': 'м', 't': 'т', 'u': 'и'
    }
    for latin, cyrillic in replacements.items():
        text = text.replace(latin, cyrillic)
        text = text.replace(latin.upper(), cyrillic.upper())
    text = re.sub(r'[\s\.\-_\,\?\*\[\]\(\)]+', '', text)
    emoji_pattern = re.compile("["
        u"\U0001F600-\U0001F64F"
        u"\U0001F300-\U0001F5FF"
        u"\U0001F680-\U0001F6FF"
        u"\U0001F1E0-\U0001F1FF"
        "]+", flags=re.UNICODE)
    text = emoji_pattern.sub(r'', text)
    return text.lower()

# ===== ФИЛЬТРЫ ССЫЛОК =====
LINK_PATTERNS = [
    r'(https?://)?(www\.)?[\w\-\.]{3,}\s*\.\s*(com|net|org|ru|by|ua|kz|pl|de|fr|it|es|nl|se|no|fi|dk|be|ch|at|jp|cn|in|br|au|ca|mx|ar|za|eg|tr|il|sa|ae|sg|my|id|ph|vn|th|kr|tw|hk|site|online|top|xyz|club|space|click|link)\b',
    r't(elegram)?\s*\.\s*(me|org|dog|is|run|pro)\s*/\s*[\w_]+',
    r'@[\w_]{5,}',
    r'(t\.me|telegram\.me|telegram\.dog|t\.is|t\.run|telegram\.pro)\s*/\s*[\w_]+',
    r't\.me/[a-zA-Z0-9_]+',
    r'https://t\.me/[a-zA-Z0-9_]+',
]
LINK_REGEX = re.compile('|'.join(LINK_PATTERNS), re.IGNORECASE)

# ===== ФИЛЬТРЫ ПРАВ =====
class IsOwnerFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id == OWNER_ID

class IsAdminOrOwnerFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if message.from_user.id == OWNER_ID:
            return True
        try:
            member = await bot.get_chat_member(message.chat.id, message.from_user.id)
            return member.status in [ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR]
        except:
            return False

# ===== ВСПОМОГАТЕЛЬНЫЕ =====
async def delete_after(delay: int, message):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except:
        pass

async def safe_reply_and_delete(message: Message, text: str, delete_in: int = 5):
    sent = await message.reply(text)
    asyncio.create_task(delete_after(delete_in, sent))

async def log_action(chat_id: int, action: str, target: str, admin: str, reason: str = ""):
    if LOG_CHANNEL_ID:
        log_text = f"📋 **{action}**\n👤 Цель: {target}\n🛡 Админ: {admin}"
        if reason:
            log_text += f"\n📝 Причина: {reason}"
        log_text += f"\n⏰ {datetime.now().strftime('%H:%M:%S %d.%m.%Y')}"
        try:
            await bot.send_message(LOG_CHANNEL_ID, log_text)
        except:
            pass

def parse_time(time_str: str) -> Optional[timedelta]:
    match = re.match(r'(\d+)([smhdw])', time_str.lower())
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    if unit == 's': return timedelta(seconds=value)
    if unit == 'm': return timedelta(minutes=value)
    if unit == 'h': return timedelta(hours=value)
    if unit == 'd': return timedelta(days=value)
    if unit == 'w': return timedelta(weeks=value)
    return None

async def check_whitelist(user_id: int, chat_id: int) -> bool:
    async with get_db() as db:
        cursor = await db.execute("SELECT 1 FROM whitelist WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
        return await cursor.fetchone() is not None

async def add_to_whitelist(user_id: int, chat_id: int):
    async with get_db() as db:
        await db.execute("INSERT OR IGNORE INTO whitelist (user_id, chat_id) VALUES (?, ?)", (user_id, chat_id))
        await db.commit()

# ===== ЗАЩИТА ОТ ПЕРЕСЫЛОК =====
async def is_forward_from_blocked_source(message: Message) -> tuple[bool, str]:
    if message.forward_from_chat:
        chat = message.forward_from_chat
        if chat.type in ["channel", "supergroup"]:
            async with get_db() as db:
                cursor = await db.execute(
                    "SELECT 1 FROM allowed_forward_sources WHERE source_id = ?",
                    (chat.id,)
                )
                if await cursor.fetchone():
                    return False, ""
            return True, f"пересылка из {chat.title or chat.type}"
    
    if message.forward_from and message.forward_from.is_bot:
        return True, f"пересылка от бота @{message.forward_from.username}"
    
    if message.forward_from and message.forward_from.username:
        scam_patterns = ["scam", "money", "invest", "crypto", "wallet", "payment", "bitcoin", "earn"]
        username_lower = message.forward_from.username.lower()
        for pattern in scam_patterns:
            if pattern in username_lower:
                return True, f"подозрительный юзернейм: @{message.forward_from.username}"
    
    return False, ""

@dp.message(F.forward_from | F.forward_from_chat)
async def block_forwards(message: Message):
    if message.from_user.id == OWNER_ID:
        return
    
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status in [ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR]:
            return
    except:
        pass
    
    async with get_db() as db:
        cursor = await db.execute("SELECT block_forwards_enabled FROM chat_settings WHERE chat_id = ?", (message.chat.id,))
        settings = await cursor.fetchone()
        if not settings or settings[0] == 0:
            return
    
    is_blocked, reason = await is_forward_from_blocked_source(message)
    
    if is_blocked:
        await message.delete()
        
        async with get_db() as db:
            await db.execute(
                "INSERT INTO warnings (user_id, chat_id, warned_by, reason) VALUES (?, ?, ?, ?)",
                (message.from_user.id, message.chat.id, 0, f"Пересылка: {reason}")
            )
            await db.commit()
        
        warn_msg = await message.answer(f"⚠️ **{message.from_user.first_name}**, {reason} запрещена!\nВы получили предупреждение.")
        asyncio.create_task(delete_after(5, warn_msg))
        await log_action(message.chat.id, "ЗАБЛОКИРОВАНА ПЕРЕСЫЛКА", message.from_user.first_name, "AUTO", reason)

# ===== КОМАНДЫ НАСТРОЙКИ ИИ =====
class AIAdminStates(StatesGroup):
    waiting_api_key = State()
    waiting_model = State()
    waiting_threshold = State()

@dp.message(Command("настроитьии", "setai", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_setup_ai(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Установить API ключ", callback_data="ai_set_key")],
        [InlineKeyboardButton(text="🤖 Сменить модель", callback_data="ai_set_model")],
        [InlineKeyboardButton(text="🎚 Установить порог", callback_data="ai_set_threshold")],
        [InlineKeyboardButton(text="✅ Включить ИИ", callback_data="ai_enable")],
        [InlineKeyboardButton(text="❌ Выключить ИИ", callback_data="ai_disable")],
        [InlineKeyboardButton(text="📊 Статус ИИ", callback_data="ai_status")]
    ])
    await message.reply("🤖 **НАСТРОЙКА ИИ МОДЕРАЦИИ**\n\nВыберите действие:", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("ai_"))
async def ai_settings_callback(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split("_")[1]
    
    if action == "set_key":
        await callback.message.edit_text("🔑 **Введите API ключ Hugging Face:**\n\n"
                                         "📌 Где взять:\n"
                                         "1. Зарегистрируйтесь на huggingface.co\n"
                                         "2. Зайдите в Settings → Access Tokens\n"
                                         "3. Создайте новый токен с ролью 'read'\n"
                                         "4. Скопируйте и вставьте сюда\n\n"
                                         "🔓 **Это бесплатно!**")
        await state.set_state(AIAdminStates.waiting_api_key)
        
    elif action == "set_model":
        await callback.message.edit_text("🤖 **Введите название модели:**\n\n"
                                         "Доступные бесплатные модели:\n"
                                         "• `cointegrated/rubert-tiny-toxicity` (русская, 35M)\n"
                                         "• `SkolkovoInstitute/rubert-toxicity` (русская, токсичность)\n"
                                         "• `mariagrandury/rubert-toxicity` (русская, 110M)\n"
                                         "• `unitary/toxic-bert` (английская)\n\n"
                                         "Вставьте название модели (можно скопировать из списка):")
        await state.set_state(AIAdminStates.waiting_model)
        
    elif action == "set_threshold":
        await callback.message.edit_text("🎚 **Установите порог чувствительности (0.0 - 1.0):**\n\n"
                                         "• 0.5 — низкий (блокирует всё подозрительное)\n"
                                         "• 0.7 — средний (рекомендуется)\n"
                                         "• 0.9 — высокий (только явный спам)\n\n"
                                         "Введите число (например, 0.7):")
        await state.set_state(AIAdminStates.waiting_threshold)
        
    elif action == "enable":
        async with get_db() as db:
            await db.execute("UPDATE chat_settings SET ai_moderation_enabled = 1 WHERE chat_id = ?", (callback.message.chat.id,))
            await db.commit()
        await callback.message.edit_text("✅ **ИИ модерация ВКЛЮЧЕНА!**\n\nВсе сообщения теперь проверяются нейросетью.")
        
    elif action == "disable":
        async with get_db() as db:
            await db.execute("UPDATE chat_settings SET ai_moderation_enabled = 0 WHERE chat_id = ?", (callback.message.chat.id,))
            await db.commit()
        await callback.message.edit_text("❌ **ИИ модерация ВЫКЛЮЧЕНА!**")
        
    elif action == "status":
        async with get_db() as db:
            cursor = await db.execute("SELECT ai_moderation_enabled, ai_api_key, ai_model, ai_threshold FROM chat_settings WHERE chat_id = ?", 
                                      (callback.message.chat.id,))
            settings = await cursor.fetchone()
        
        if settings:
            enabled = "✅ Включена" if settings[0] else "❌ Выключена"
            has_key = "🔑 Установлен" if settings[1] else "⚠️ Не установлен"
            model = settings[2] if settings[2] else "cointegrated/rubert-tiny-toxicity"
            threshold = settings[3] if settings[3] else 0.7
            
            status_text = f"🤖 **СТАТУС ИИ МОДЕРАЦИИ**\n\n"
            status_text += f"Состояние: {enabled}\n"
            status_text += f"API ключ: {has_key}\n"
            status_text += f"Модель: `{model}`\n"
            status_text += f"Порог: {threshold}\n\n"
            status_text += "💡 **Совет:** Для работы ИИ нужен бесплатный API ключ Hugging Face"
            await callback.message.edit_text(status_text)
    
    await callback.answer()

@dp.message(AIAdminStates.waiting_api_key)
async def process_api_key(message: Message, state: FSMContext):
    api_key = message.text.strip()
    
    # Сохраняем ключ
    async with get_db() as db:
        await db.execute("UPDATE chat_settings SET ai_api_key = ? WHERE chat_id = ?", (api_key, message.chat.id))
        await db.commit()
    
    # Устанавливаем в модератор
    await ai_moderator.set_api_key(api_key)
    
    await message.reply("✅ **API ключ сохранён!**\n\nИИ модерация готова к работе.\nВключите её командой `/настроитьии` → Включить ИИ")
    await state.clear()

@dp.message(AIAdminStates.waiting_model)
async def process_model(message: Message, state: FSMContext):
    model = message.text.strip()
    
    async with get_db() as db:
        await db.execute("UPDATE chat_settings SET ai_model = ? WHERE chat_id = ?", (model, message.chat.id))
        await db.commit()
    
    await ai_moderator.set_model(model)
    
    await message.reply(f"✅ **Модель изменена:** `{model}`")
    await state.clear()

@dp.message(AIAdminStates.waiting_threshold)
async def process_threshold(message: Message, state: FSMContext):
    try:
        threshold = float(message.text.strip().replace(",", "."))
        if not 0 <= threshold <= 1:
            raise ValueError
    except:
        await message.reply("❌ **Ошибка!** Введите число от 0.0 до 1.0 (например, 0.7)")
        return
    
    async with get_db() as db:
        await db.execute("UPDATE chat_settings SET ai_threshold = ? WHERE chat_id = ?", (threshold, message.chat.id))
        await db.commit()
    
    await message.reply(f"✅ **Порог чувствительности установлен:** {threshold}")
    await state.clear()

# ===== MIDDLEWARE С ИИ МОДЕРАЦИЕЙ =====
class ModerationMiddleware:
    def __init__(self):
        self.user_messages: Dict[int, List[float]] = {}
        self.join_counter: List[float] = []
    
    async def __call__(self, handler, event: Message, data: dict):
        if not event.chat or event.chat.type not in ["group", "supergroup"]:
            return await handler(event, data)
        
        if event.from_user.id == OWNER_ID:
            return await handler(event, data)
        
        try:
            member = await bot.get_chat_member(event.chat.id, event.from_user.id)
            if member.status in [ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR]:
                async with get_db() as db:
                    await db.execute("INSERT OR REPLACE INTO admin_activity (user_id, last_seen) VALUES (?, ?)",
                                    (event.from_user.id, datetime.now()))
                    await db.commit()
                return await handler(event, data)
        except:
            pass
        
        if not await check_whitelist(event.from_user.id, event.chat.id):
            async with get_db() as db:
                cursor = await db.execute("SELECT captcha_enabled FROM chat_settings WHERE chat_id = ?", (event.chat.id,))
                settings = await cursor.fetchone()
            if settings and settings[0] == 1:
                await event.delete()
                await safe_reply_and_delete(event, "🔐 **Требуется проверка**\nНажмите кнопку 'Я человек' при входе в чат", 5)
                return
        
        # Антифлуд
        async with get_db() as db:
            cursor = await db.execute("SELECT antiflood_enabled, antiflood_limit FROM chat_settings WHERE chat_id = ?", (event.chat.id,))
            settings = await cursor.fetchone()
        
        if settings and settings[0] == 1:
            limit = settings[1] if settings[1] else 5
            now = datetime.now().timestamp()
            user_id = event.from_user.id
            
            if user_id not in self.user_messages:
                self.user_messages[user_id] = []
            
            self.user_messages[user_id] = [t for t in self.user_messages[user_id] if now - t < 2]
            
            if len(self.user_messages[user_id]) >= limit:
                muted_until = datetime.now() + timedelta(hours=1)
                permissions = ChatPermissions(can_send_messages=False)
                try:
                    await bot.restrict_chat_member(event.chat.id, user_id, permissions, until_date=muted_until)
                    async with get_db() as db:
                        await db.execute("INSERT OR REPLACE INTO muted_users VALUES (?, ?, ?, ?, ?)",
                                        (user_id, event.chat.id, muted_until, "Авто-флуд", 0))
                        await db.commit()
                    await safe_reply_and_delete(event, f"🚫 **ФЛУД**\nМут 1 час ({limit}+ сообщений/2сек)", 10)
                except:
                    pass
                self.user_messages[user_id] = []
                return
            self.user_messages[user_id].append(now)
        
        # Проверка контента
        text_to_check = event.text or event.caption or ""
        if text_to_check:
            normalized = normalize_text(text_to_check)
            cleaned_text = re.sub(r'\s+', ' ', normalized)
            
            # Проверка ссылок
            if LINK_REGEX.search(cleaned_text):
                await event.delete()
                await safe_reply_and_delete(event, f"⚠️ Ссылки и реклама запрещены!", 4)
                await log_action(event.chat.id, "УДАЛЕНО", event.from_user.first_name, "AUTO", "ссылка")
                return
            
            # Проверка на приглашения в ботов
            bot_invite_patterns = [r't\.me/[a-zA-Z0-9_]+bot', r'напиши боту', r'бот разошлет']
            for pattern in bot_invite_patterns:
                if re.search(pattern, cleaned_text, re.IGNORECASE):
                    await event.delete()
                    await safe_reply_and_delete(event, f"🤖 Реклама ботов запрещена!", 4)
                    return
            
            # Анти-капс
            letters = [c for c in text_to_check if c.isalpha()]
            if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.7 and len(text_to_check) > 20:
                await event.delete()
                await safe_reply_and_delete(event, f"🔇 Пожалуйста, не пишите КАПСОМ!", 4)
                return
            
            # Чёрный список слов
            async with get_db() as db:
                cursor = await db.execute("SELECT word FROM banned_words")
                banned_words = await cursor.fetchall()
            
            for banned_word in banned_words:
                if banned_word[0] in cleaned_text:
                    await event.delete()
                    await safe_reply_and_delete(event, f"🤬 Сообщение удалено (запрещённое слово)", 5)
                    await log_action(event.chat.id, "УДАЛЕНО", event.from_user.first_name, "AUTO", f"слово: {banned_word[0]}")
                    return
            
            # === ИИ МОДЕРАЦИЯ ===
            async with get_db() as db:
                cursor = await db.execute("SELECT ai_moderation_enabled, ai_threshold FROM chat_settings WHERE chat_id = ?", (event.chat.id,))
                ai_settings = await cursor.fetchone()
            
            if ai_settings and ai_settings[0] == 1:
                threshold = ai_settings[1] if ai_settings[1] else 0.7
                
                is_toxic, confidence, reason = await ai_moderator.check_text(text_to_check, threshold)
                
                if is_toxic:
                    await event.delete()
                    await safe_reply_and_delete(event, f"🧠 **ИИ модерация**\nСообщение удалено: {reason}", 5)
                    await log_action(event.chat.id, "УДАЛЕНО (ИИ)", event.from_user.first_name, "AI", reason)
                    return
        
        return await handler(event, data)

dp.message.middleware(ModerationMiddleware())

# ===== КАРМА =====
@dp.message(F.reply_to_message)
async def karma_system(message: Message):
    target = message.reply_to_message.from_user
    sender = message.from_user
    
    if target.id == sender.id:
        return
    
    text = message.text.lower().strip() if message.text else ""
    
    karma_change = 0
    reason = None
    
    if text in ["+", "+респект", "спасибо", "респект", "благодарю", "good"]:
        karma_change = 1
        reason = "респект"
    elif text in ["-", "-токсик", "токсик", "плохо", "bad"]:
        karma_change = -1
        reason = "токсик"
    else:
        return
    
    async with get_db() as db:
        cursor = await db.execute("SELECT last_karma_change FROM karma WHERE user_id = ? AND chat_id = ?", (sender.id, message.chat.id))
        last = await cursor.fetchone()
        if last and last[0]:
            last_time = datetime.fromisoformat(last[0])
            if datetime.now() - last_time < timedelta(minutes=1):
                await safe_reply_and_delete(message, "⏳ Нельзя менять карму чаще раза в минуту", 3)
                return
        
        await db.execute(
            "INSERT INTO karma (user_id, chat_id, karma, last_karma_change) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, chat_id) DO UPDATE SET karma = karma + ?, last_karma_change = ?",
            (target.id, message.chat.id, karma_change, datetime.now(), karma_change, datetime.now())
        )
        await db.commit()
        
        cursor = await db.execute("SELECT karma FROM karma WHERE user_id = ? AND chat_id = ?", (target.id, message.chat.id))
        new_karma = (await cursor.fetchone())[0]
    
    if new_karma <= -5:
        permissions = ChatPermissions(
            can_send_messages=True, can_send_media_messages=False, can_send_polls=False,
            can_send_other_messages=False, can_add_web_page_previews=False, can_invite_users=True
        )
        await bot.restrict_chat_member(message.chat.id, target.id, permissions)
        await message.answer(f"⚠️ **{target.first_name}**, карма {new_karma} → режим 'Подозрительный' (без медиа)")
    
    emoji = "❤️" if karma_change > 0 else "💔"
    await safe_reply_and_delete(message, f"{emoji} **Карма {target.first_name}**: {new_karma:+d} ({reason})", 5)

@dp.message(Command("top", "топ", prefix="!/"))
async def cmd_top_karma(message: Message):
    async with get_db() as db:
        cursor = await db.execute("SELECT user_id, karma FROM karma WHERE chat_id = ? ORDER BY karma DESC LIMIT 10", (message.chat.id,))
        top_users = await cursor.fetchall()
    
    if not top_users:
        await message.reply("📊 Нет данных о карме. Ставьте +респект полезным людям!")
        return
    
    top_list = []
    for i, user in enumerate(top_users, 1):
        try:
            user_obj = await bot.get_chat_member(message.chat.id, user[0])
            name = user_obj.user.first_name
        except:
            name = f"User_{user[0]}"
        top_list.append(f"{i}. **{name}** — {user[1]} ❤️")
    
    result = "🏆 **ТОП-10 ПО КАРМЕ:**\n\n" + "\n".join(top_list)
    await message.reply(result)

@dp.message(Command("karma", "карма", prefix="!/"))
async def cmd_my_karma(message: Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    async with get_db() as db:
        cursor = await db.execute("SELECT karma FROM karma WHERE user_id = ? AND chat_id = ?", (target.id, message.chat.id))
        result = await cursor.fetchone()
    karma = result[0] if result else 0
    emoji = "❤️" if karma >= 0 else "💔"
    await message.reply(f"{emoji} **Карма {target.first_name}**: {karma:+d}")

# ===== HONEYPOT КАПЧА =====
captcha_data = {}
active_joins: List[float] = []
RAID_JOIN_LIMIT = 5
RAID_LOCK_DURATION = 600

async def check_raid(chat_id: int) -> bool:
    global active_joins
    now = datetime.now().timestamp()
    active_joins = [t for t in active_joins if now - t < 10]
    if len(active_joins) >= RAID_JOIN_LIMIT:
        permissions = ChatPermissions(can_send_messages=False, can_send_media_messages=False, can_send_polls=False,
                                      can_send_other_messages=False, can_add_web_page_previews=False, can_invite_users=True)
        await bot.set_chat_permissions(chat_id, permissions)
        await bot.send_message(chat_id, "🚨 **АТАКА БОТОВ!** Чат закрыт на 10 минут.")
        asyncio.create_task(unlock_after_raid(chat_id))
        return True
    return False

async def unlock_after_raid(chat_id: int):
    await asyncio.sleep(RAID_LOCK_DURATION)
    permissions = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_polls=True,
                                  can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True)
    await bot.set_chat_permissions(chat_id, permissions)
    await bot.send_message(chat_id, "🔓 Чат разблокирован.")

@dp.message(F.new_chat_members)
async def honeypot_captcha(message: Message):
    async with get_db() as db:
        cursor = await db.execute("SELECT captcha_enabled FROM chat_settings WHERE chat_id = ?", (message.chat.id,))
        settings = await cursor.fetchone()
    if not settings or settings[0] == 0:
        return
    
    global active_joins
    active_joins.append(datetime.now().timestamp())
    if await check_raid(message.chat.id):
        return
    
    for new_member in message.new_chat_members:
        if new_member.id == bot.id:
            continue
        
        permissions = ChatPermissions(can_send_messages=False)
        await bot.restrict_chat_member(message.chat.id, new_member.id, permissions)
        
        captcha_id = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        hidden_button_text = "\u200C" + "verify" + "\u200C"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я человек", callback_data=f"captcha_real_{new_member.id}_{captcha_id}")],
            [InlineKeyboardButton(text=hidden_button_text, callback_data=f"captcha_honeypot_{new_member.id}_{captcha_id}")]
        ])
        
        captcha_data[new_member.id] = {"captcha_id": captcha_id, "chat_id": message.chat.id}
        
        await message.answer(f"🔐 **Добро пожаловать, {new_member.first_name}!**\n\nНажмите кнопку **«Я человек»**.\nЕсли не пройдёте за 5 минут — исключение.",
                            reply_markup=keyboard)
        asyncio.create_task(kick_if_not_verified(message.chat.id, new_member.id))

async def kick_if_not_verified(chat_id: int, user_id: int):
    await asyncio.sleep(300)
    if user_id in captcha_data:
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await asyncio.sleep(1)
            await bot.unban_chat_member(chat_id, user_id)
        except:
            pass
        finally:
            if user_id in captcha_data:
                del captcha_data[user_id]

@dp.callback_query(F.data.startswith(("captcha_real_", "captcha_honeypot_")))
async def process_captcha(callback_query: CallbackQuery):
    data_parts = callback_query.data.split("_")
    captcha_type = data_parts[1]
    user_id = int(data_parts[2])
    captcha_id = data_parts[3]
    
    if callback_query.from_user.id != user_id:
        await callback_query.answer("❌ Это не ваша капча!", show_alert=True)
        return
    
    if captcha_type == "honeypot":
        await bot.ban_chat_member(callback_query.message.chat.id, user_id)
        await callback_query.message.edit_text(f"🔨 **БОТ ОБНАРУЖЕН И ЗАБАНЕН!**")
        await callback_query.answer("❌ Вы бот!", show_alert=True)
        if user_id in captcha_data:
            del captcha_data[user_id]
        return
    
    if user_id not in captcha_data or captcha_data[user_id]["captcha_id"] != captcha_id:
        await callback_query.answer("❌ Ошибка", show_alert=True)
        return
    
    permissions = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_polls=True,
                                  can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True)
    await bot.restrict_chat_member(callback_query.message.chat.id, user_id, permissions)
    await add_to_whitelist(user_id, callback_query.message.chat.id)
    
    if user_id in captcha_data:
        del captcha_data[user_id]
    
    await callback_query.message.delete()
    await callback_query.answer("✅ Добро пожаловать!")
    await bot.send_message(callback_query.message.chat.id, f"🎉 {callback_query.from_user.first_name} прошёл проверку!")

# ===== КОМАНДЫ АДМИНОВ =====
@dp.message(Command("addслово", "addword", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_add_word(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await safe_reply_and_delete(message, "⚡ **Использование:** `!addслово слово`", 5)
        return
    word = normalize_text(args[1])
    async with get_db() as db:
        try:
            await db.execute("INSERT INTO banned_words (word, added_by) VALUES (?, ?)", (word, message.from_user.id))
            await db.commit()
            await safe_reply_and_delete(message, f"✅ Слово `{word}` добавлено", 3)
        except:
            await safe_reply_and_delete(message, f"ℹ️ Слово `{word}` уже есть", 3)

@dp.message(Command("delслово", "delword", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_remove_word(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await safe_reply_and_delete(message, "⚡ **Использование:** `!delслово слово`", 5)
        return
    word = normalize_text(args[1])
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM banned_words WHERE word = ?", (word,))
        await db.commit()
        if cursor.rowcount > 0:
            await safe_reply_and_delete(message, f"🗑️ Слово `{word}` удалено", 3)
        else:
            await safe_reply_and_delete(message, f"❌ Слово `{word}` не найдено", 3)

@dp.message(Command("список", "words", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_list_words(message: Message):
    async with get_db() as db:
        cursor = await db.execute("SELECT word FROM banned_words ORDER BY word")
        words = await cursor.fetchall()
    if not words:
        await message.reply("📂 Чёрный список пуст")
        return
    word_list = "\n".join([f"— `{w[0]}`" for w in words[:50]])
    await message.reply(f"🚫 **Запрещённые слова:**\n\n{word_list}")

@dp.message(Command("mute", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_mute(message: Message):
    if not message.reply_to_message:
        await safe_reply_and_delete(message, "❌ Ответьте на сообщение нарушителя!\n📌 Пример: `!mute 1h спам`", 5)
        return
    target = message.reply_to_message.from_user
    args = message.text.split()
    if target.id == OWNER_ID:
        await safe_reply_and_delete(message, "👑 Нельзя замутить владельца", 3)
        return
    duration = parse_time(args[1]) if len(args) > 1 else timedelta(hours=1)
    reason = " ".join(args[2:]) if len(args) > 2 else "Нарушение правил"
    if not duration and len(args) > 1:
        reason = " ".join(args[1:])
        duration = timedelta(hours=1)
    muted_until = datetime.now() + duration
    permissions = ChatPermissions(can_send_messages=False)
    try:
        await bot.restrict_chat_member(message.chat.id, target.id, permissions, until_date=muted_until)
        async with get_db() as db:
            await db.execute("INSERT OR REPLACE INTO muted_users VALUES (?, ?, ?, ?, ?)",
                           (target.id, message.chat.id, muted_until, reason, message.from_user.id))
            await db.commit()
        time_str = f"{duration.total_seconds() // 3600}ч" if duration.total_seconds() >= 3600 else f"{duration.total_seconds() // 60}м"
        if duration.total_seconds() < 60:
            time_str = f"{duration.total_seconds()}с"
        await message.reply(f"🔇 **Мут**\n👤 {html.quote(target.first_name)}\n⏱ {time_str}\n📝 {reason}")
    except Exception as e:
        await safe_reply_and_delete(message, f"❌ Ошибка: {str(e)}", 5)

@dp.message(Command("unmute", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_unmute(message: Message):
    if not message.reply_to_message:
        await safe_reply_and_delete(message, "❌ Ответьте на сообщение пользователя", 5)
        return
    target = message.reply_to_message.from_user
    permissions = ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_polls=True,
                                  can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True)
    try:
        await bot.restrict_chat_member(message.chat.id, target.id, permissions)
        async with get_db() as db:
            await db.execute("DELETE FROM muted_users WHERE user_id = ? AND chat_id = ?", (target.id, message.chat.id))
            await db.commit()
        await message.reply(f"🔊 **Снят мут**\n👤 {html.quote(target.first_name)}")
    except Exception as e:
        await safe_reply_and_delete(message, f"❌ Ошибка: {str(e)}", 5)

@dp.message(Command("kick", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_kick(message: Message):
    if not message.reply_to_message:
        await safe_reply_and_delete(message, "❌ Ответьте на сообщение нарушителя", 5)
        return
    target = message.reply_to_message.from_user
    if target.id == OWNER_ID:
        await safe_reply_and_delete(message, "👑 Нельзя кикнуть владельца", 3)
        return
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await asyncio.sleep(0.5)
        await bot.unban_chat_member(message.chat.id, target.id)
        await message.reply(f"👢 **Кик**\n👤 {html.quote(target.first_name)}")
    except Exception as e:
        await safe_reply_and_delete(message, f"❌ Ошибка: {str(e)}", 5)

@dp.message(Command("ban", prefix="!/"), IsOwnerFilter())
async def cmd_ban(message: Message):
    if not message.reply_to_message:
        await safe_reply_and_delete(message, "❌ Ответьте на сообщение пользователя", 5)
        return
    target = message.reply_to_message.from_user
    await bot.ban_chat_member(message.chat.id, target.id)
    await message.reply(f"🔨 **БАН**\n👤 {html.quote(target.first_name)}")

@dp.message(Command("pin", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_pin(message: Message):
    if not message.reply_to_message:
        await safe_reply_and_delete(message, "❌ Ответьте на сообщение для закрепа", 5)
        return
    try:
        disable_notify = "silent" in message.text.lower()
        await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id, disable_notification=disable_notify)
        await message.reply("📌 Закреплено")
        if not disable_notify:
            await message.delete()
    except Exception as e:
        await safe_reply_and_delete(message, f"❌ Ошибка: {str(e)}", 5)

@dp.message(Command("unpin", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_unpin(message: Message):
    try:
        if message.reply_to_message:
            await bot.unpin_chat_message(message.chat.id, message.reply_to_message.message_id)
        else:
            await bot.unpin_all_chat_messages(message.chat.id)
        await message.reply("📍 Откреплено")
        await message.delete()
    except Exception as e:
        await safe_reply_and_delete(message, f"❌ Ошибка: {str(e)}", 5)

@dp.message(Command("lock", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_lock(message: Message):
    try:
        await bot.set_chat_permissions(message.chat.id, ChatPermissions(
            can_send_messages=False, can_send_media_messages=False, can_send_polls=False,
            can_send_other_messages=False, can_add_web_page_previews=False, can_invite_users=True))
        await message.reply("🔒 **Чат закрыт**")
    except Exception as e:
        await safe_reply_and_delete(message, f"❌ Ошибка: {str(e)}", 5)

@dp.message(Command("unlock", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_unlock(message: Message):
    try:
        await bot.set_chat_permissions(message.chat.id, ChatPermissions(
            can_send_messages=True, can_send_media_messages=True, can_send_polls=True,
            can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True))
        await message.reply("🔓 **Чат открыт**")
    except Exception as e:
        await safe_reply_and_delete(message, f"❌ Ошибка: {str(e)}", 5)

@dp.message(Command("warn", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_warn(message: Message):
    if not message.reply_to_message:
        await safe_reply_and_delete(message, "❌ Ответьте на сообщение нарушителя", 5)
        return
    target = message.reply_to_message.from_user
    reason = " ".join(message.text.split()[1:]) if len(message.text.split()) > 1 else "Нарушение правил"
    async with get_db() as db:
        await db.execute("INSERT INTO warnings (user_id, chat_id, warned_by, reason) VALUES (?, ?, ?, ?)",
                        (target.id, message.chat.id, message.from_user.id, reason))
        thirty_days_ago = datetime.now() - timedelta(days=30)
        cursor = await db.execute("SELECT COUNT(*) FROM warnings WHERE user_id = ? AND chat_id = ? AND warned_at > ?",
                                 (target.id, message.chat.id, thirty_days_ago))
        warn_count = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT warn_limit FROM chat_settings WHERE chat_id = ?", (message.chat.id,))
        settings = await cursor.fetchone()
    warn_limit = settings[0] if settings else 3
    await message.reply(f"⚠️ **Предупреждение #{warn_count}**\n👤 {html.quote(target.first_name)}\n📝 {reason}")
    if warn_count >= warn_limit:
        muted_until = datetime.now() + timedelta(hours=1)
        permissions = ChatPermissions(can_send_messages=False)
        await bot.restrict_chat_member(message.chat.id, target.id, permissions, until_date=muted_until)
        await message.answer(f"🔨 **Авто-мут** {html.quote(target.first_name)} на 1 час ({warn_count}/{warn_limit} варнов)")

@dp.message(Command("антифорвард", "blockforwards", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_block_forwards(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await safe_reply_and_delete(message, "⚡ **Использование:** `!антифорвард 1` (вкл) или `!антифорвард 0` (выкл)", 5)
        return
    
    enabled = 1 if args[1] == "1" else 0
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO chat_settings (chat_id, block_forwards_enabled) VALUES (?, ?)",
            (message.chat.id, enabled)
        )
        await db.commit()
    
    status = "включена" if enabled else "выключена"
    await message.reply(f"🔄 Блокировка пересылок {status}")

# ===== СИСТЕМА ЗАМЕТОК =====
@dp.message(Command("setnote", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_set_note(message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await safe_reply_and_delete(message, "⚡ **Использование:** `!setnote триггер текст`", 5)
        return
    trigger = args[1].lower().strip()
    content = args[2]
    async with get_db() as db:
        await db.execute("INSERT OR REPLACE INTO notes (chat_id, trigger, content, created_by) VALUES (?, ?, ?, ?)",
                        (message.chat.id, trigger, content, message.from_user.id))
        await db.commit()
    await safe_reply_and_delete(message, f"✅ Заметка `{trigger}` сохранена!", 3)

@dp.message(Command("delnote", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_del_note(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await safe_reply_and_delete(message, "⚡ **Использование:** `!delnote триггер`", 5)
        return
    trigger = args[1].lower().strip()
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM notes WHERE chat_id = ? AND trigger = ?", (message.chat.id, trigger))
        await db.commit()
        if cursor.rowcount > 0:
            await safe_reply_and_delete(message, f"🗑️ Заметка `{trigger}` удалена", 3)
        else:
            await safe_reply_and_delete(message, f"❌ Заметка `{trigger}` не найдена", 3)

@dp.message(Command("notes", prefix="!/"), IsAdminOrOwnerFilter())
async def cmd_list_notes(message: Message):
    async with get_db() as db:
        cursor = await db.execute("SELECT trigger, content FROM notes WHERE chat_id = ?", (message.chat.id,))
        notes = await cursor.fetchall()
    if not notes:
        await message.reply("📝 Нет заметок")
        return
    notes_list = "\n".join([f"• `!{n[0]}` — {n[1][:50]}..." for n in notes[:20]])
    await message.reply(f"📚 **Заметки:**\n\n{notes_list}")

@dp.message(F.text & F.text.startswith('!'))
async def handle_notes(message: Message):
    trigger = message.text[1:].lower().strip()
    async with get_db() as db:
        cursor = await db.execute("SELECT content FROM notes WHERE chat_id = ? AND trigger = ?", (message.chat.id, trigger))
        note = await cursor.fetchone()
    if note:
        await message.reply(note[0])

# ===== БИРЖА =====
class AdStates(StatesGroup):
    waiting_photo = State()
    waiting_description = State()
    waiting_price = State()

@dp.message(Command("sell", "продам", prefix="!/"))
async def cmd_sell(message: Message):
    async with get_db() as db:
        cursor = await db.execute("SELECT ad_cooldown_hours FROM chat_settings WHERE chat_id = ?", (message.chat.id,))
        settings = await cursor.fetchone()
        cooldown_hours = settings[0] if settings else 12
        cursor = await db.execute("SELECT last_ad_time FROM ads_limit WHERE user_id = ? AND chat_id = ?",
                                 (message.from_user.id, message.chat.id))
        last_ad = await cursor.fetchone()
        if last_ad and last_ad[0]:
            last_time = datetime.fromisoformat(last_ad[0])
            hours_passed = (datetime.now() - last_time).total_seconds() / 3600
            if hours_passed < cooldown_hours:
                remaining = cooldown_hours - hours_passed
                await message.reply(f"⏳ Следующее объявление через {remaining:.1f} часов")
                return
    await message.reply("📸 Отправьте фото товара")
    await AdStates.waiting_photo.set()

@dp.message(AdStates.waiting_photo, F.photo)
async def ad_photo(message: Message, state: FSMContext):
    await state.update_data(photo_id=message.photo[-1].file_id)
    await message.reply("📝 Напишите описание")
    await AdStates.waiting_description.set()

@dp.message(AdStates.waiting_description)
async def ad_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await message.reply("💰 Укажите цену")
    await AdStates.waiting_price.set()

@dp.message(AdStates.waiting_price)
async def ad_price(message: Message, state: FSMContext):
    data = await state.get_data()
    async with get_db() as db:
        await db.execute("INSERT OR REPLACE INTO ads_limit (user_id, chat_id, last_ad_time, ads_count) VALUES (?, ?, ?, COALESCE((SELECT ads_count + 1 FROM ads_limit WHERE user_id = ? AND chat_id = ?), 1))",
                        (message.from_user.id, message.chat.id, datetime.now(), message.from_user.id, message.chat.id))
        await db.commit()
        cursor = await db.execute("SELECT karma FROM karma WHERE user_id = ? AND chat_id = ?", (message.from_user.id, message.chat.id))
        karma_row = await cursor.fetchone()
        karma = karma_row[0] if karma_row else 0
    karma_emoji = "⭐" * min(3, max(0, karma // 10)) if karma > 0 else "⚠️"
    ad_text = f"🛍 **НОВОЕ ОБЪЯВЛЕНИЕ** {karma_emoji}\n\n👤 {html.quote(message.from_user.first_name)}\n📝 {data['description']}\n💰 {message.text}\n📊 Карма: {karma:+d}"
    await bot.send_photo(message.chat.id, data['photo_id'], caption=ad_text)
    await message.reply("✅ Объявление опубликовано!")
    await state.clear()

# ===== ИНТЕЛЛЕКТУАЛЬНАЯ КОМАНДА /КОМАНДЫ =====
USER_HELP_TEXT = """
🛍️ **ЭКОСИСТЕМА ЧАТА — КОМАНДЫ**
━━━━━━━━━━━━━━━━━━━━

▶️ **!sell** — создать объявление
▶️ **!top** — топ-10 по карме
▶️ **!karma** — показать свою карму

🔥 **СИСТЕМА КАРМЫ:**
Ответь на сообщение словом `+` или `спасибо` — поднимешь репутацию.
Ответь словом `-` или `токсик` — при плохой репутации.

━━━━━━━━━━━━━━━━━━━━
"""

ADMIN_HELP_TEXT = """
🛡️ **ПАНЕЛЬ УПРАВЛЕНИЯ BREST GUARD PRO v5.0** 🛡️
━━━━━━━━━━━━━━━━━━━━

🛠️ **1. БАЗОВАЯ МОДЕРАЦИЯ:**
• `!mute [время] [причина]` — временный мут
• `!unmute` (ответом) — снять мут
• `!kick` (ответом) — выгнать
• `!warn [причина]` — предупреждение
• `!del` (ответом) — тихо удалить

📌 **2. УПРАВЛЕНИЕ СООБЩЕНИЯМИ:**
• `!pin` (ответом) — закрепить
• `!unpin` — открепить
• `!lock` / `!unlock` — закрыть/открыть чат

🤬 **3. ФИЛЬТРЫ СЛОВ:**
• `!addслово [слово]` — добавить в ЧС
• `!delслово [слово]` — удалить из ЧС
• `!список` — показать все слова

📝 **4. ЗАМЕТКИ:**
• `!setnote [триггер] [текст]` — создать
• `!delnote [триггер]` — удалить
• `!notes` — список

🔄 **5. ЗАЩИТА ОТ ПЕРЕСЫЛОК:**
• `!антифорвард 1` — включить
• `!антифорвард 0` — выключить

🧠 **6. ИИ МОДЕРАЦИЯ (НЕЙРОСЕТЬ):**
• `!настроитьии` — меню настройки ИИ
• Можно установить API ключ от Hugging Face (бесплатно)
• ИИ проверяет сообщения на токсичность и спам

⚙️ **7. ТОЛЬКО СОЗДАТЕЛЬ:**
• `!ban` (ответом) — перманентный бан
• `!clear [число]` — очистка чата

━━━━━━━━━━━━━━━━━━━━
"""

@dp.message(Command("команды", "help", "start", prefix="!/"))
async def smart_help_command(message: Message):
    is_admin = False
    if message.from_user.id == OWNER_ID:
        is_admin = True
    else:
        try:
            member = await bot.get_chat_member(message.chat.id, message.from_user.id)
            if member.status in [ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR]:
                is_admin = True
        except:
            pass
    
    if is_admin:
        try:
            await message.delete()
            await bot.send_message(chat_id=message.from_user.id, text=ADMIN_HELP_TEXT, parse_mode="Markdown")
            confirm = await message.answer("📨 **Админ-панель отправлена в ЛС!**")
            asyncio.create_task(delete_after(3, confirm))
        except TelegramForbiddenError:
            warning = await message.answer("⚠️ **Начните диалог с ботом в ЛС, чтобы получить админ-панель!**")
            asyncio.create_task(delete_after(10, warning))
    else:
        help_msg = await message.reply(USER_HELP_TEXT, parse_mode="Markdown")
        asyncio.create_task(delete_after(15, help_msg))
        try:
            await message.delete()
        except:
            pass

@dp.message(Command("админпанель", "admin", prefix="!/"), IsAdminOrOwnerFilter())
async def admin_panel_command(message: Message):
    try:
        await message.delete()
        await bot.send_message(chat_id=message.from_user.id, text=ADMIN_HELP_TEXT, parse_mode="Markdown")
        confirm = await message.answer("📨 **Админ-панель отправлена в ЛС!**")
        asyncio.create_task(delete_after(3, confirm))
    except TelegramForbiddenError:
        await message.reply("⚠️ **Начните диалог с ботом в ЛС!**")

@dp.message(Command("стата", "stats", prefix="!/"), IsAdminOrOwnerFilter())
async def bot_stats_command(message: Message):
    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM banned_words")
        banned_words_count = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM muted_users WHERE muted_until > datetime('now')")
        muted_count = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM whitelist WHERE chat_id = ?", (message.chat.id,))
        whitelist_count = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM notes WHERE chat_id = ?", (message.chat.id,))
        notes_count = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT SUM(karma) FROM karma WHERE chat_id = ? AND karma > 0", (message.chat.id,))
        total_karma = (await cursor.fetchone())[0] or 0
        cursor = await db.execute("SELECT ai_moderation_enabled FROM chat_settings WHERE chat_id = ?", (message.chat.id,))
        ai_settings = await cursor.fetchone()
    
    ai_status = "✅ Включена" if ai_settings and ai_settings[0] else "❌ Выключена"
    
    stats_text = f"""
📊 **СТАТИСТИКА БОТА**
━━━━━━━━━━━━━━━━━━━━

🛡 **Модерация:**
• Запрещённых слов: {banned_words_count}
• Активных мутов: {muted_count}
• Прошли капчу: {whitelist_count}

📝 **Контент:**
• Заметок: {notes_count}
• Всего кармы: {total_karma} ❤️

🧠 **ИИ модерация:** {ai_status}

🤖 **Статус:** ✅ Работает

━━━━━━━━━━━━━━━━━━━━
"""
    await message.reply(stats_text, parse_mode="Markdown")

# ===== ЗАПУСК =====
async def restore_muted_users():
    async with get_db() as db:
        cursor = await db.execute("SELECT user_id, chat_id, muted_until FROM muted_users WHERE muted_until > datetime('now')")
        muted = await cursor.fetchall()
    for user_id, chat_id, muted_until in muted:
        try:
            muted_until_dt = datetime.fromisoformat(muted_until)
            permissions = ChatPermissions(can_send_messages=False)
            await bot.restrict_chat_member(chat_id, user_id, permissions, until_date=muted_until_dt)
        except:
            pass

async def main():
    await init_database()
    print("✅ База данных инициализирована")
    
    # Загружаем сохранённый API ключ
    async with get_db() as db:
        cursor = await db.execute("SELECT ai_api_key, ai_model, ai_threshold FROM chat_settings WHERE chat_id IS NOT NULL LIMIT 1")
        settings = await cursor.fetchone()
        if settings and settings[0]:
            await ai_moderator.set_api_key(settings[0])
            if settings[1]:
                await ai_moderator.set_model(settings[1])
    
    await restore_muted_users()
    print("✅ Муты восстановлены")
    print("=" * 60)
    print("🤖 BREST GUARD ULTIMATE v5.0 (С ИИ МОДЕРАЦИЕЙ) ЗАПУЩЕН")
    print(f"👑 Owner ID: {OWNER_ID}")
    print("📊 Функции: Капча | Карма | Антифлуд | Заметки | Биржа | ИИ модерация")
    print("=" * 60)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
