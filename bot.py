"""Telegram suggestions bot. Python 3.10+, standard library only."""
import json
import html
import logging
import sqlite3
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUBMIT = '📨 Отправить в предложку'
CANCEL = '🗑 Очистить черновик'
KEYBOARD = {'keyboard': [[SUBMIT], [CANCEL]], 'resize_keyboard': True}
SUPPORTED = ('text', 'photo', 'video', 'animation', 'document', 'audio', 'voice', 'video_note', 'sticker')
STATES = {'pending': 'Ожидает решения', 'publishing': 'Публикация начата — повтор заблокирован',
          'published': 'Опубликовано', 'partial': 'Опубликовано частично — проверьте канал',
          'rejected': 'Отклонено', 'deleted': 'Удалено из канала',
          'uncertain': 'Результат отправки неизвестен. Проверьте канал вручную',
          'delivering': 'Передача в группу прервана — проверьте сообщения',
          'delivery_failed': 'Не удалось передать заявку полностью'}


class APIError(Exception):
    def __init__(self, code, description):
        self.code = code
        super().__init__(description)


class Telegram:
    def __init__(self, token):
        self.base = 'https://api.telegram.org/bot' + token + '/'

    def __call__(self, method, **data):
        request = urllib.request.Request(self.base + method,
            data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                result = json.load(response)
        except urllib.error.HTTPError as e:
            try:
                result = json.loads(e.read())
            except (ValueError, OSError):
                raise RuntimeError('Неопределенный ответ Telegram') from None
        except (OSError, ValueError):
            # Never include request URL: it contains the bot token.
            raise RuntimeError('Нет подтверждения от Telegram') from None
        if not result.get('ok'):
            raise APIError(result.get('error_code', 0), result.get('description', 'Ошибка API'))
        return result['result']


class Bot:
    def __init__(self, config, api=None, db_path=None):
        self.cfg = config
        self.api = api or Telegram(config['bot_token'])
        self.group = config.get('moderation_group_id', 0)
        self.channel = config.get('channel_id', 0)
        self.db = sqlite3.connect(db_path or ROOT / 'bot.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v INTEGER);
            CREATE TABLE IF NOT EXISTS drafts(uid INTEGER, mid INTEGER, author TEXT,
                PRIMARY KEY(uid, mid));
            CREATE TABLE IF NOT EXISTS submissions(
                id INTEGER PRIMARY KEY, uid INTEGER, author TEXT, source TEXT,
                state TEXT, group_id INTEGER, channel_id TEXT, preview TEXT DEFAULT '[]',
                card INTEGER, published TEXT DEFAULT '[]', created REAL, actor INTEGER);
        ''')
        # Non-destructive migration for existing databases.
        for table, column in [('drafts', 'content'), ('submissions', 'payload')]:
            cols = {r['name'] for r in self.db.execute(f'PRAGMA table_info({table})')}
            if column not in cols:
                self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} TEXT')
        self.db.execute("UPDATE submissions SET state='uncertain' WHERE state='publishing'")
        self.db.commit()

    def say(self, chat, text, **kwargs):
        return self.api('sendMessage', chat_id=chat, text=text, **kwargs)

    def safe(self, method, **kwargs):
        try:
            return self.api(method, **kwargs)
        except Exception:
            logging.warning('Не удалось выполнить %s', method)

    def row(self, sid):
        return self.db.execute('SELECT * FROM submissions WHERE id=?', (sid,)).fetchone()

    def claim(self, sid, before, after, actor):
        with self.db:
            return self.db.execute('UPDATE submissions SET state=?,actor=? WHERE id=? AND state=?',
                (after, actor, sid, before)).rowcount == 1

    def set_state(self, sid, state):
        with self.db:
            self.db.execute('UPDATE submissions SET state=? WHERE id=?', (state, sid))

    def markup(self, row):
        sid = row['id']
        buttons = []
        if row['state'] == 'pending':
            buttons.append([{'text': '✅ Отправить', 'callback_data': f'publish:{sid}'},
                            {'text': '❌ Не отправлять', 'callback_data': f'reject:{sid}'}])
        if json.loads(row['published']) and row['state'] != 'deleted':
            buttons.append([{'text': '🗑 Удалить из канала', 'callback_data': f'delete:{sid}'}])
        buttons.append([{'text': '👤 Кто отправил', 'callback_data': f'author:{sid}'}])
        return {'inline_keyboard': buttons}

    def card_text(self, row):
        result = f"Заявка №{row['id']}\n{STATES.get(row['state'], row['state'])}"
        if row['actor']:
            result += f"\nРешение: участник ID {row['actor']}"
        result += '\n\n✉️ Чтобы ответить автору, ответьте на эту карточку или сообщение заявки в группе.'
        return result

    def refresh(self, sid):
        row = self.row(sid)
        if row['card']:
            self.safe('editMessageText', chat_id=row['group_id'], message_id=row['card'],
                      text=self.card_text(row), reply_markup=self.markup(row))

    def moderator_reply(self, m):
        """Relay a moderator's reply to a submission card/material to its author."""
        if m['chat']['id'] != self.group or not m.get('reply_to_message'):
            return False
        reply_mid = m['reply_to_message']['message_id']
        candidates = self.db.execute(
            'SELECT * FROM submissions WHERE group_id=? AND card IS NOT NULL ORDER BY id DESC',
            (self.group,)).fetchall()
        row = next((r for r in candidates if r['card'] == reply_mid or
                    reply_mid in json.loads(r['preview'] or '[]')), None)
        if row is None:
            return False
        sender = m.get('from', {})
        if not sender.get('id') or sender.get('is_bot'):
            return True
        if m.get('text', '').startswith('/'):
            return True
        try:
            member = self.api('getChatMember', chat_id=self.group, user_id=sender['id'])
            if member['status'] not in ('creator', 'administrator', 'member') and not (
                    member['status'] == 'restricted' and member.get('is_member')):
                return True
        except Exception:
            return True
        if not any(k in m for k in SUPPORTED) or m.get('has_protected_content'):
            self.say(self.group, 'Такой ответ нельзя передать. Используйте текст, фото или другой поддерживаемый файл.',
                     reply_parameters={'message_id': m['message_id']})
            return True
        try:
            heading = '📨 Вам пришло сообщение от администрации канала!'
            if 'text' in m:
                # Send as one message, with no moderator identity or forwarded attribution.
                body = m['text']
                if len(heading) + 2 + len(body) > 4096:
                    raise ValueError('Ответ слишком длинный (максимум 4096 символов вместе с заголовком).')
                self.say(row['uid'], heading + '\n\n' + body)
            elif any(k in m for k in ('photo', 'video', 'animation', 'document', 'audio', 'voice')):
                # Caption stays attached to the original media, rather than becoming a separate message.
                caption = m.get('caption', '')
                combined = heading + ('\n\n' + caption if caption else '')
                if len(combined) > 1024:
                    raise ValueError('Подпись слишком длинная (максимум 1024 символа вместе с заголовком).')
                self.api('copyMessage', chat_id=row['uid'], from_chat_id=self.group,
                         message_id=m['message_id'], caption=combined)
            else:
                # Stickers and video notes cannot have captions in Telegram.
                self.say(row['uid'], heading)
                self.api('copyMessage', chat_id=row['uid'], from_chat_id=self.group,
                         message_id=m['message_id'])
        except ValueError as e:
            self.say(self.group, str(e), reply_parameters={'message_id': m['message_id']})
        except APIError as e:
            logging.warning('Ответ на заявку %s не доставлен: %s', row['id'], e.code)
            self.say(self.group, f'Не удалось доставить ответ по заявке №{row["id"]}. Возможно, пользователь заблокировал бота.',
                     reply_parameters={'message_id': m['message_id']})
        except Exception:
            logging.warning('Результат доставки ответа на заявку %s неизвестен', row['id'])
            self.say(self.group, f'Результат отправки ответа по заявке №{row["id"]} неизвестен. Не повторяйте без проверки.',
                     reply_parameters={'message_id': m['message_id']})
        else:
            self.say(self.group, f'✉️ Ответ по заявке №{row["id"]} отправлен автору.',
                     reply_parameters={'message_id': m['message_id']})
        return True

    def message(self, m):
        chat, uid = m['chat']['id'], m.get('from', {}).get('id')
        text = m.get('text', '')
        command = text.split()[0].split('@')[0] if text else ''
        if command == '/id':
            self.say(chat, f'ID этого чата: {chat}')
            return
        if m['chat']['type'] in ('group', 'supergroup') and self.moderator_reply(m):
            return
        if m['chat']['type'] != 'private' or not uid or m['from'].get('is_bot'):
            return
        if command in ('/start', '/help'):
            self.say(chat, 'Отправьте текст, фото, видео, файлы, стикеры или голосовые, '
                'затем нажмите «Отправить в предложку». До 100 сообщений в заявке. '
                'Дождитесь загрузки всех фото перед отправкой.\n\n'
                'В канале ваше имя не добавляется. Участники группы модерации могут '
                'увидеть ваше имя, username и ID. Содержимое публикуется как есть: '
                'не добавляйте личные данные, если хотите сохранить анонимность.\n'
                '/cancel — очистить черновик; /send — отправить.', reply_markup=KEYBOARD)
        elif command == '/cancel' or text == CANCEL:
            with self.db:
                self.db.execute('DELETE FROM drafts WHERE uid=?', (uid,))
            self.say(chat, 'Черновик очищен.', reply_markup=KEYBOARD)
        elif command == '/send' or text == SUBMIT:
            self.submit(uid)
        elif command.startswith('/'):
            self.say(chat, 'Используйте /help, /send или /cancel.')
        elif any(k in m for k in SUPPORTED) and not m.get('has_protected_content'):
            count = self.db.execute('SELECT count(*) FROM drafts WHERE uid=?', (uid,)).fetchone()[0]
            if count >= 100:
                self.say(chat, 'В черновике уже 100 сообщений. Отправьте его или очистите.')
                return
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO drafts(uid,mid,author,content) VALUES(?,?,?,?)',
                                (uid, m['message_id'], json.dumps(m['from'], ensure_ascii=False),
                                 json.dumps({'kind': next(k for k in SUPPORTED if k in m),
                                             'text': m.get('text', m.get('caption', ''))}, ensure_ascii=False)))
            if count == 0:
                self.say(chat, 'Добавлено в черновик. Можно добавить ещё материалы, затем отправить.',
                         reply_markup=KEYBOARD)
        else:
            self.say(chat, 'Этот тип не поддерживается. Пришлите текст, фото, видео, '
                           'файл, стикер, аудио, кружок или голосовое.')

    def submit(self, uid):
        if not self.group or not self.channel:
            self.say(uid, 'Бот пока не настроен владельцем.')
            return
        rows = self.db.execute('SELECT * FROM drafts WHERE uid=? ORDER BY mid', (uid,)).fetchall()
        if not rows:
            self.say(uid, 'Черновик пуст. Сначала отправьте материалы.')
            return
        last = self.db.execute('SELECT max(created) FROM submissions WHERE uid=?', (uid,)).fetchone()[0]
        if last and time.time() - last < self.cfg.get('cooldown_seconds', 30):
            self.say(uid, 'Подождите немного перед следующей заявкой. Черновик сохранён.')
            return
        # Commit ownership before the network call. Repeated /send cannot re-deliver this draft.
        with self.db:
            sid = self.db.execute('''INSERT INTO submissions
                (uid,author,source,state,group_id,channel_id,created,payload) VALUES(?,?,?,?,?,?,?,?)''',
                (uid, rows[0]['author'], json.dumps([r['mid'] for r in rows]), 'delivering',
                 self.group, str(self.channel), time.time(),
                 json.dumps([json.loads(r['content']) if r['content'] else None for r in rows], ensure_ascii=False))).lastrowid
            self.db.execute('DELETE FROM drafts WHERE uid=?', (uid,))
        try:
            copied = self.api('copyMessages', chat_id=self.group, from_chat_id=uid,
                              message_ids=[r['mid'] for r in rows])
            ids = [r['message_id'] for r in copied]
            with self.db:
                self.db.execute('UPDATE submissions SET preview=?,state=? WHERE id=?',
                    (json.dumps(ids), 'pending' if len(ids) == len(rows) else 'delivery_failed', sid))
            row = self.row(sid)
            args = {'reply_markup': self.markup(row)}
            if ids:
                args['reply_parameters'] = {'message_id': ids[0], 'allow_sending_without_reply': True}
            card = self.say(self.group, self.card_text(row), **args)
            with self.db:
                self.db.execute('UPDATE submissions SET card=? WHERE id=?', (card['message_id'], sid))
            self.say(uid, f'Заявка №{sid} передана модераторам.' if row['state'] == 'pending'
                     else f'Заявка №{sid} передана не полностью. Сообщите владельцу бота.')
        except Exception:
            logging.warning('Передача заявки %s прервана; автоматического повтора нет', sid)
            self.safe('sendMessage', chat_id=uid, text=f'Передача заявки №{sid} не подтверждена. '
                      'Свяжитесь с владельцем бота, прежде чем отправлять её заново.')

    def answer(self, q, text, alert=False):
        self.safe('answerCallbackQuery', callback_query_id=q['id'], text=text[:200], show_alert=alert)

    def callback(self, q):
        try:
            action, sid = q.get('data', '').split(':')
            row = self.row(int(sid))
        except (ValueError, TypeError):
            return
        if not row or not q.get('message') or q['message']['chat']['id'] != row['group_id']:
            self.answer(q, 'Кнопка доступна только в группе модерации.', True)
            return
        if q['message']['message_id'] != row['card']:
            self.answer(q, 'Это не карточка заявки.', True)
            return
        uid = q['from']['id']
        try:
            member = self.api('getChatMember', chat_id=row['group_id'], user_id=uid)
        except Exception:
            self.answer(q, 'Не удалось проверить участие в группе.', True)
            return
        if member['status'] not in ('creator', 'administrator', 'member') and not (
                member['status'] == 'restricted' and member.get('is_member')):
            self.answer(q, 'Доступ только участникам группы.', True)
            return
        sid = row['id']
        if action == 'author':
            author = json.loads(row['author'])
            name = ' '.join(filter(None, [author.get('first_name'), author.get('last_name')]))[:85]
            username = '@' + author['username'] if author.get('username') else 'username не указан'
            self.answer(q, f"Автор: {name}\n{username}\nID: {row['uid']}", True)
            return
        if action == 'publish':
            if not self.claim(sid, 'pending', 'publishing', uid):
                self.answer(q, 'Заявка уже обработана или отправляется.')
                self.refresh(sid)
                return
            self.answer(q, 'Публикую…')
            ids = []
            preview_ids = json.loads(row['preview'])
            payload = json.loads(row['payload']) if row['payload'] else []
            link = '<a href="https://t.me/+VR9_vNJ91MhmY2U1">Подслушано Гимназия</a>'
            try:
                if len(preview_ids) == 1 and len(payload) == 1 and payload[0]:
                    item = payload[0]
                    content = html.escape(item.get('text', ''))
                    formatted = '📩 Анонимная предложка\n\n' + content + '\n\n' + link
                    if item['kind'] == 'text':
                        if len(formatted.encode('utf-16-le')) // 2 > 4096:
                            raise APIError(400, 'Текст слишком длинный для оформления одним сообщением')
                        sent = self.api('sendMessage', chat_id=row['channel_id'],
                                        text=formatted, parse_mode='HTML',
                                        link_preview_options={'is_disabled': True})
                    elif item['kind'] in ('photo', 'video', 'animation', 'document', 'audio', 'voice'):
                        if len(formatted.encode('utf-16-le')) // 2 > 1024:
                            raise APIError(400, 'Подпись слишком длинная для одного медиа')
                        sent = self.api('copyMessage', chat_id=row['channel_id'],
                                        from_chat_id=row['group_id'], message_id=preview_ids[0],
                                        caption=formatted, parse_mode='HTML')
                    else:
                        # Stickers and video notes do not support captions.
                        sent = self.api('copyMessage', chat_id=row['channel_id'],
                                        from_chat_id=row['group_id'], message_id=preview_ids[0])
                    ids = [sent['message_id']]
                    state = 'published'
                else:
                    # Multiple messages cannot generally be merged into one Telegram message.
                    result = self.api('copyMessages', chat_id=row['channel_id'],
                                      from_chat_id=row['group_id'], message_ids=preview_ids)
                    ids = [m['message_id'] for m in result]
                    state = 'published' if len(ids) == len(preview_ids) else 'partial'
                with self.db:
                    self.db.execute('UPDATE submissions SET state=?,published=? WHERE id=?',
                                    (state, json.dumps(ids), sid))
            except APIError as e:
                self.set_state(sid, 'partial' if ids else
                               ('pending' if 400 <= e.code < 500 else 'uncertain'))
                logging.warning('Публикация %s: ошибка API %s: %s', sid, e.code, e)
            except Exception:
                self.set_state(sid, 'uncertain')
            self.refresh(sid)
        elif action == 'reject':
            ok = self.claim(sid, 'pending', 'rejected', uid)
            self.answer(q, 'Отклонено.' if ok else 'Заявка уже обработана.')
            self.refresh(sid)
        elif action == 'delete':
            ids = json.loads(row['published'])
            if not ids or row['state'] == 'deleted':
                self.answer(q, 'Нет опубликованных сообщений для удаления.')
                return
            self.answer(q, 'Удаляю из канала…')
            remaining = list(ids)
            for mid in ids:
                try:
                    self.api('deleteMessage', chat_id=row['channel_id'], message_id=mid)
                except APIError as e:
                    if 'message to delete not found' not in str(e).lower():
                        continue
                except Exception:
                    continue
                remaining.remove(mid)
                with self.db:
                    self.db.execute('UPDATE submissions SET published=? WHERE id=?',
                                    (json.dumps(remaining), sid))
            if not remaining:
                self.set_state(sid, 'deleted')
            else:
                self.safe('sendMessage', chat_id=row['group_id'], text=f'Заявка №{sid}: '
                    'не всё удалено. Проверьте права бота и срок 48 часов; при сбое связи '
                    'нажмите «Удалить» ещё раз.', reply_parameters={'message_id': row['card']})
            self.refresh(sid)

    def run(self):
        me = self.api('getMe')
        hook = self.api('getWebhookInfo')
        if hook.get('url'):
            raise RuntimeError('У бота настроен webhook. Используйте отдельного нового бота.')
        if self.group and self.channel:
            group_member = self.api('getChatMember', chat_id=self.group, user_id=me['id'])
            channel_member = self.api('getChatMember', chat_id=self.channel, user_id=me['id'])
            if group_member['status'] != 'administrator':
                raise RuntimeError('Назначьте бота администратором группы.')
            if not channel_member.get('can_post_messages') or not channel_member.get('can_delete_messages'):
                raise RuntimeError('В канале нужны права публикации и удаления сообщений.')
            if self.api('getChat', chat_id=self.channel)['type'] != 'channel':
                raise RuntimeError('channel_id должен указывать на канал.')
        print(f"Бот работает: https://t.me/{me['username']}?start=suggest", flush=True)
        if not self.group or not self.channel:
            print('Режим настройки: получите /id в группе и канале, заполните config.json.', flush=True)
        for row in self.db.execute('SELECT id FROM submissions WHERE card IS NOT NULL').fetchall():
            if self.row(row['id'])['state'] == 'uncertain':
                self.refresh(row['id'])
        while True:
            offset = self.db.execute("SELECT v FROM meta WHERE k='offset'").fetchone()
            try:
                updates = self.api('getUpdates', offset=offset[0] if offset else 0, timeout=25,
                                   allowed_updates=['message', 'callback_query', 'channel_post'])
            except APIError as e:
                if e.code in (401, 409):
                    raise RuntimeError('Неверный токен или запущена вторая копия бота.') from None
                time.sleep(3)
                continue
            except Exception:
                time.sleep(3)
                continue
            for update in updates:
                # Consume before effects: a crash cannot replay a successful publication.
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO meta VALUES('offset',?)", (update['update_id'] + 1,))
                try:
                    if 'callback_query' in update:
                        self.callback(update['callback_query'])
                    elif 'message' in update:
                        self.message(update['message'])
                    elif 'channel_post' in update:
                        self.message(update['channel_post'])
                except Exception:
                    logging.warning('Не удалось обработать событие %s; автоматического повтора нет', update['update_id'])


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    path = ROOT / 'config.json'
    if not path.exists():
        print('Скопируйте config.example.json в config.json и заполните настройки.')
        return
    try:
        config = json.loads(path.read_text(encoding='utf-8-sig'))
        if ':' not in config.get('bot_token', ''):
            raise ValueError('Вставьте токен от BotFather в config.json.')
        Bot(config).run()
    except KeyboardInterrupt:
        print('\nБот остановлен.')
    except (ValueError, RuntimeError, APIError) as e:
        print('Ошибка:', str(e))


if __name__ == '__main__':
    main()
