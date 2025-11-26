# --- START OF FILE main.py ---

import logging
import os
from telegram.ext import ApplicationBuilder, MessageHandler, filters

import config
from bot_state import StateManager
import bot_ai
from bot_handlers import handle_message, background_tasks

# Настройка логирования
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s', 
    datefmt='%Y-%m-%d %H:%M:%S', 
    handlers=[logging.StreamHandler()]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)

logger = logging.getLogger(__name__)

def main():
    # Проверки перед запуском
    if not os.path.exists(config.PROMPT_FILE): 
        logger.critical(f"❌ Файл промпта '{config.PROMPT_FILE}' не найден!"); return
    if not config.TELEGRAM_TOKEN or "YOUR_TOKEN" in config.TELEGRAM_TOKEN: 
        logger.critical("❌ TELEGRAM_TOKEN не установлен!"); return
    if not bot_ai.init_ai(config.API_KEYS): 
        logger.critical("❌ Не удалось инициализировать модель Gemini."); return

    # Настройка прокси
    os.environ['http_proxy'] = config.PROXY_URL
    os.environ['https_proxy'] = config.PROXY_URL
    os.environ['HTTP_PROXY'] = config.PROXY_URL
    os.environ['HTTPS_PROXY'] = config.PROXY_URL
    
    # Инициализация состояния
    state_manager = StateManager(config.STATE_FILE)
    
    app = ApplicationBuilder().token(config.TELEGRAM_TOKEN).build()
    
    # Dependency Injection: Передаем зависимости в bot_data
    # Это разрывает круг импортов: handlers не нужно импортировать bot_ai напрямую
    app.bot_data["state_manager"] = state_manager
    app.bot_data["process_user_input"] = bot_ai.process_user_input
    app.bot_data["retrieve_memory"] = bot_ai.retrieve_memory
    app.bot_data["generate_reflection"] = bot_ai.generate_reflection

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    if app.job_queue:
        app.job_queue.run_repeating(background_tasks, interval=config.CHECK_INTERVAL_SECONDS, first=10)
    
    logger.info(f"🚀 Бот v{config.BOT_VERSION} запущен.")
    app.run_polling()

if __name__ == '__main__':
    main()