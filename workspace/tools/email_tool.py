"""Кастомный инструмент: Почта Mail.ru.

Формат кастомного тула:
- литеральный манифест TOOL (name, description, methods с params) — читается ботом через AST;
- каждый метод = python-функция, принимающая аргументы по имени (см. params);
- консольный запуск: python3 email_tool.py <method> [--arg value ...] -> печатает JSON результат.
"""

import imaplib
import smtplib
import email
import json
import os
import re
import sys
import argparse
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

TOOL = {
    "name": "email",
    "description": "Почта Mail.ru: непрочитанные, чтение письма, отправка.",
    "methods": {
        "get_unread": {
            "description": "Список непрочитанных писем",
            "params": [
                {"name": "limit", "type": "int", "required": False, "default": 5,
                 "description": "сколько писем показать"}
            ],
        },
        "read": {
            "description": "Прочитать письмо по id",
            "params": [
                {"name": "msg_id", "type": "str", "required": True,
                 "description": "id письма (из get_unread)"}
            ],
        },
        "send": {
            "description": "Отправить письмо",
            "params": [
                {"name": "to", "type": "str", "required": True, "description": "email получателя"},
                {"name": "subject", "type": "str", "required": True, "description": "тема письма"},
                {"name": "body", "type": "str", "required": True, "description": "текст письма"},
            ],
        },
    },
}

IMAP_SERVER = 'imap.mail.ru'
SMTP_SERVER = 'smtp.mail.ru'
SMTP_PORT = 465

# EMAIL_USER/EMAIL_PASS — из env процесса бота (load_dotenv в config.py)
USER = os.getenv('EMAIL_USER', '')
PASSWORD = os.getenv('EMAIL_PASS', '')


def decode_str(header_val):
    if not header_val:
        return ''
    decoded_list = decode_header(header_val)
    result = ''
    for decoded_string, encoding in decoded_list:
        if isinstance(decoded_string, bytes):
            if encoding:
                try:
                    result += decoded_string.decode(encoding)
                except Exception:
                    result += decoded_string.decode('utf-8', errors='ignore')
            else:
                result += decoded_string.decode('utf-8', errors='ignore')
        else:
            result += str(decoded_string)
    return result


def strip_html(html_content):
    text = re.sub(r'<style.*?>.*?</style>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script.*?>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    lines = [line.strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def get_unread(limit=5):
    """Список непрочитанных писем. Возвращает list[dict]."""
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(USER, PASSWORD)
    mail.select('inbox')
    status, messages = mail.search(None, 'UNSEEN')
    if status != 'OK':
        return []
    msg_nums = messages[0].split()
    results = []
    for num in msg_nums[-int(limit):]:
        res, data = mail.fetch(num, '(BODY[HEADER.FIELDS (FROM SUBJECT DATE)])')
        if res == 'OK':
            msg = email.message_from_bytes(data[0][1])
            subject = decode_str(msg['Subject'])
            from_ = decode_str(msg['From'])
            date = msg['Date']
            results.append({'id': num.decode(), 'from': from_, 'subject': subject, 'date': date})
    mail.logout()
    return results


def read(msg_id):
    """Прочитать письмо. Возвращает dict с body."""
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(USER, PASSWORD)
    mail.select('inbox')
    res, data = mail.fetch(str(msg_id).encode(), '(RFC822)')
    if res != 'OK':
        return None
    raw_email = data[0][1]
    msg = email.message_from_bytes(raw_email)
    subject = decode_str(msg['Subject'])
    from_ = decode_str(msg['From'])
    date = msg['Date']
    plain_body = ''
    html_body = ''
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get('Content-Disposition'))
            if 'attachment' not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or 'utf-8'
                    try:
                        text_content = payload.decode(charset, errors='ignore')
                    except Exception:
                        text_content = payload.decode('utf-8', errors='ignore')
                    if content_type == 'text/plain':
                        plain_body += text_content + '\n'
                    elif content_type == 'text/html':
                        html_body += text_content + '\n'
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or 'utf-8'
            try:
                text_content = payload.decode(charset, errors='ignore')
            except Exception:
                text_content = payload.decode('utf-8', errors='ignore')
            if msg.get_content_type() == 'text/html':
                html_body = text_content
            else:
                plain_body = text_content
    mail.logout()
    final_body = plain_body.strip() if plain_body.strip() else strip_html(html_body)
    return {'id': msg_id, 'from': from_, 'subject': subject, 'date': date, 'body': final_body}


def send(to, subject, body):
    """Отправить письмо. Возвращает True."""
    msg = MIMEMultipart()
    msg['From'] = USER
    msg['To'] = to
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain', 'utf-8'))
    server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
    server.login(USER, PASSWORD)
    server.send_message(msg)
    server.quit()
    return True


_METHODS = {"get_unread": get_unread, "read": read, "send": send}


def _main():
    if not USER or not PASSWORD:
        print(json.dumps({"ok": False, "error": "EMAIL_USER/EMAIL_PASS не найдены в env"}, ensure_ascii=False))
        sys.exit(1)

    method = sys.argv[1] if len(sys.argv) > 1 else None
    if not method or method not in _METHODS:
        print(json.dumps({"ok": False, "error": f"укажи метод: {', '.join(_METHODS)}"}, ensure_ascii=False))
        sys.exit(1)

    parser = argparse.ArgumentParser(add_help=False)
    schema = TOOL["methods"][method]
    for p in schema["params"]:
        arg = f"--{p['name']}"
        if p["type"] == "int":
            parser.add_argument(arg, type=int, default=p.get("default"))
        else:
            parser.add_argument(arg, default=p.get("default"))
        if p.get("required"):
            parser.set_defaults(**{p["name"]: None})
    argv = sys.argv[2:]
    names = [f"--{p['name']}" for p in schema["params"]]
    filtered = [a for a in argv if a in names or a.startswith("--") is False or any(a == n for n in names)]
    ns, _ = parser.parse_known_args(filtered)
    kwargs = {p["name"]: getattr(ns, p["name"]) for p in schema["params"]}

    for p in schema["params"]:
        if p.get("required") and kwargs[p["name"]] is None:
            print(json.dumps({"ok": False, "error": f"обязательный аргумент --{p['name']}"}, ensure_ascii=False))
            sys.exit(1)

    try:
        result = _METHODS[method](**kwargs)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))


if __name__ == '__main__':
    _main()