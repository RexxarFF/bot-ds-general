# ============================================================
# ЦВЕТА
# ============================================================

COLOR_PANEL = 0x19B9D1
COLOR_CONTROL = 0x5865F2
COLOR_PENDING = 0xF2B84B
COLOR_ACCEPTED = 0x59B77A
COLOR_REJECTED = 0xD85C5C
COLOR_LOG = 0x66757F


# ============================================================
# СТАТУС БОТА
# ============================================================

BOT_ACTIVITY = "за FunFernus"


# ============================================================
# ПУБЛИЧНАЯ ПАНЕЛЬ ПО УМОЛЧАНИЮ
# ============================================================

DEFAULT_PANEL_TITLE = "🎮 Добро пожаловать на FunFernus!"

DEFAULT_PANEL_DESCRIPTION = (
    "Рады видеть вас в нашем сообществе! Чтобы начать игру, "
    "выполните несколько простых шагов:\n\n"
    "• Ознакомьтесь с правилами сервера.\n"
    "• Подайте заявку на вступление.\n"
    "• Дождитесь ответа администрации.\n\n"
    "✨ Спасибо за понимание и интерес к нашему проекту!"
)

DEFAULT_PANEL_FOOTER = "FunFernus • Приём заявок"
DEFAULT_PANEL_IMAGE_URL = ""
DEFAULT_PANEL_THUMBNAIL_URL = ""
DEFAULT_PANEL_COLOR = COLOR_PANEL
DEFAULT_BUTTON_LABEL = "Подать заявку"
DEFAULT_BUTTON_EMOJI = "📋"
DEFAULT_BUTTON_STYLE = "green"


# ============================================================
# ВОПРОСЫ ФОРМЫ DISCORD
# Discord позволяет разместить до пяти текстовых полей в одной форме.
# ============================================================

QUESTIONS = [
    {
        "label": "Minecraft-ник",
        "embed_name": "🎮 Minecraft-ник",
        "placeholder": "Например: Felix_Wraith",
        "min_length": 3,
        "max_length": 16,
        "paragraph": False,
        "required": True,
    },
    {
        "label": "Возраст",
        "embed_name": "🎂 Возраст",
        "placeholder": "Например: 15",
        "min_length": 1,
        "max_length": 3,
        "paragraph": False,
        "required": True,
    },
    {
        "label": "Расскажи немного о себе",
        "embed_name": "👋 О себе",
        "placeholder": "Чем занимаешься и как давно играешь...",
        "min_length": 10,
        "max_length": 1000,
        "paragraph": True,
        "required": True,
    },
    {
        "label": "Почему хочешь играть у нас?",
        "embed_name": "💭 Почему FunFernus?",
        "placeholder": "Почему тебя заинтересовал сервер?",
        "min_length": 10,
        "max_length": 1000,
        "paragraph": True,
        "required": True,
    },
    {
        "label": "Что планируешь делать на сервере?",
        "embed_name": "🏗️ Планы на сервер",
        "placeholder": "Строительство, торговля, ролевое взаимодействие...",
        "min_length": 10,
        "max_length": 1000,
        "paragraph": True,
        "required": True,
    },
]

MODAL_TITLE = "Заявка на FunFernus"


# ============================================================
# СЛУЖЕБНЫЕ ПОЛЯ ЗАЯВКИ
# ============================================================

FIELD_APPLICANT = "👤 Заявитель"
FIELD_NICKNAME = "🎮 Minecraft-ник"
FIELD_USER_ID = "🆔 Discord ID"


# ============================================================
# КАРТОЧКИ ЗАЯВОК
# ============================================================

REVIEW_EMBED_TITLE = "📨 Новая заявка"
REVIEW_EMBED_DESCRIPTION = "Заявка ожидает решения администрации."
REVIEW_FOOTER_PENDING = "Статус: ожидает рассмотрения"
REVIEW_FOOTER_ACCEPTED = "Статус: заявка принята"
REVIEW_FOOTER_REJECTED = "Статус: заявка отклонена"
ACCEPTED_EMBED_TITLE = "✅ Заявка принята"
REJECTED_EMBED_TITLE = "🚫 Заявка отклонена"


# ============================================================
# ЛИЧНЫЕ СООБЩЕНИЯ
# ============================================================

ACCEPT_DM_TITLE = "🎉 Твоя заявка принята!"
ACCEPT_DM_TEXT = (
    "Поздравляем! Администрация **FunFernus** одобрила твою заявку.\n\n"
    "Ты добавлен в whitelist сервера. Добро пожаловать!"
)

REJECT_DM_TITLE = "🚫 Твоя заявка отклонена"
REJECT_DM_TEXT = (
    "К сожалению, администрация **FunFernus** отклонила твою заявку.\n"
    "Причина указана ниже."
)

DM_FOOTER = "FunFernus"
SERVER_ADDRESS = "mc.funfernus.ru"
SERVER_VERSION = "26.1.2"


# ============================================================
# РЕКОМЕНДУЕМЫЕ КАТЕГОРИИ И КАНАЛЫ
# ============================================================

PUBLIC_CATEGORY_NAME = "📌 FUNFERNUS"
RULES_CHANNEL_NAME = "📜・правила"
APPLICATION_PANEL_CHANNEL_NAME = "📝・подать-заявку"
NEWS_CHANNEL_NAME = "📢・новости"

STAFF_CATEGORY_NAME = "🛡 STAFF"
CONTROL_CHANNEL_NAME = "⚙️・управление-ботом"
REVIEW_CHANNEL_NAME = "📨・рассмотрение-заявок"
LOG_CHANNEL_NAME = "📑・логи-заявок"
RCON_CHANNEL_NAME = "🖥・rcon-консоль"
CONFIG_CHANNEL_NAME = "🔒・bot-config"
CONFIG_CHANNEL_TOPIC = "FUNFERNUS_CONFIG_V2 — служебный канал. Не удалять сообщения бота."
