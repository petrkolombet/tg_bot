# --- START OF FILE main.py ---

import logging
import os
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters
from telegram.request import HTTPXRequest

import config
from bot_state import StateManager
import bot_ai
from bot_handlers import handle_message, handle_voice, handle_file, handle_server, handle_sum, handle_think, background_tasks, handle_stop, handle_restart, handle_models, handle_callback

# Настройка логирования
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s', 
    datefmt='%Y-%m-%d %H:%M:%S', 
    handlers=[logging.StreamHandler()]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

def main():
    # Проверки перед запуском
    if not os.path.exists(config.PROMPT_FILE): 
        logger.critical(f"❌ Файл промпта '{config.PROMPT_FILE}' не найден!"); return
    if not config.TELEGRAM_TOKEN or "YOUR_TOKEN" in config.TELEGRAM_TOKEN: 
        logger.critical("❌ TELEGRAM_TOKEN не установлен!"); return

    # Настройка прокси для прямого доступа в интернет (не для Gemini)
    if config.PROXY_URL:
        os.environ['http_proxy'] = config.PROXY_URL
        os.environ['https_proxy'] = config.PROXY_URL
        os.environ['HTTP_PROXY'] = config.PROXY_URL
        os.environ['HTTPS_PROXY'] = config.PROXY_URL
    # Gemini proxy на localhost — не через прокси
    os.environ['no_proxy'] = '127.0.0.1,localhost'
    os.environ['NO_PROXY'] = '127.0.0.1,localhost'
    
    # Инициализация состояния
    state_manager = StateManager(config.STATE_FILE)
    
    app = ApplicationBuilder().token(config.TELEGRAM_TOKEN).concurrent_updates(True)
    if config.TELEGRAM_PROXY:
        app = app.request(HTTPXRequest(proxy=config.TELEGRAM_PROXY, read_timeout=35, connect_timeout=20))
        app = app.get_updates_request(HTTPXRequest(proxy=config.TELEGRAM_PROXY, read_timeout=35, connect_timeout=20))
    app = app.build()
    
    # Dependency Injection: Передаем зависимости в bot_data
    # Это разрывает круг импортов: handlers не нужно импортировать bot_ai напрямую
    app.bot_data["state_manager"] = state_manager
    app.bot_data["process_user_input"] = bot_ai.process_user_input
    app.bot_data["retrieve_memory"] = bot_ai.retrieve_memory
    app.bot_data["generate_reflection"] = bot_ai.generate_reflection
    app.bot_data["search_web"] = bot_ai.search_web
    app.bot_data["transcribe_voice"] = bot_ai.transcribe_voice
    app.bot_data["update_longterm_summary"] = bot_ai.update_longterm_summary

    app.add_handler(CommandHandler("server", handle_server))
    app.add_handler(CommandHandler("sum", handle_sum))
    app.add_handler(CommandHandler("think", handle_think))
    app.add_handler(CommandHandler("stop", handle_stop))
    app.add_handler(CommandHandler("restart", handle_restart))
    app.add_handler(CommandHandler("models", handle_models))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VIDEO, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    
    if app.job_queue:
        app.job_queue.run_repeating(background_tasks, interval=config.CHECK_INTERVAL_SECONDS, first=10)
    
    logger.info(f"🚀 Бот v{config.BOT_VERSION} запущен.")
    app.run_polling()

if __name__ == '__main__':
    main()