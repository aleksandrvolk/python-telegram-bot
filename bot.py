import os
import asyncio
import io
import json
from pathlib import Path
import html
from urllib.parse import quote, unquote
import docker
import paramiko
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters, Defaults
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).with_name('.env'))

class DockerBot:
    def __init__(self):
        self.bot_token = os.getenv('BOT_TOKEN')
        # Состояние пользователей для пошагового ввода SSH данных
        self.user_states = {}
        # Сохраненные сервера по пользователям
        self.user_servers = {}
        # Глобальные сервера из ENV (общие для всех пользователей, без права удаления из меню)
        self.env_servers = self._load_env_servers()
        print(f"ENV servers loaded: {len(self.env_servers)}")
        # Опционально: ограничить доступ определенным пользователям
        # self.allowed_users = [int(user_id) for user_id in os.getenv('ALLOWED_USERS', '').split(',') if user_id]
        # Настройка Docker клиента для работы с socket
        try:
            # Проверяем доступность socket
            if not os.path.exists('/var/run/docker.sock'):
                raise Exception("Docker socket не найден: /var/run/docker.sock")
            
            # Используем прямой путь к socket
            self.docker_client = docker.DockerClient(base_url='unix:///var/run/docker.sock')
            # Проверяем подключение к Docker
            self.docker_client.ping()
            print("Docker подключение успешно установлено")
        except Exception as e:
            print(f"Ошибка подключения к Docker: {e}")
            print("Убедитесь, что Docker socket смонтирован в контейнер")
            raise
        
    async def get_containers(self):
        """Получить список контейнеров"""
        try:
            containers = self.docker_client.containers.list(all=True)
            result = []
            for container in containers:
                result.append({
                    'name': container.name,
                    'status': container.status,
                    'image': container.image.tags[0] if container.image.tags else container.image.short_id
                })
            return result
        except Exception as e:
            print(f"Ошибка при получении контейнеров: {e}")
            return []
    
    async def get_container_stats(self):
        """Получить статистику контейнеров"""
        try:
            containers = self.docker_client.containers.list()
            if not containers:
                return "Нет запущенных контейнеров"
            
            stats_text = ""
            for container in containers:
                stats = container.stats(stream=False)
                cpu_percent = self._calculate_cpu_percent(stats)
                memory_percent = self._calculate_memory_percent(stats)
                
                stats_text += f"🟢 {container.name}\n"
                stats_text += f"   CPU: {cpu_percent:.1f}%\n"
                stats_text += f"   Память: {memory_percent:.1f}%\n\n"
            
            return stats_text
        except Exception as e:
            print(f"Ошибка при получении статистики: {e}")
            return "Ошибка при получении статистики"
    
    def _calculate_cpu_percent(self, stats):
        """Вычислить процент использования CPU"""
        try:
            cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - stats['precpu_stats']['cpu_usage']['total_usage']
            system_delta = stats['cpu_stats']['system_cpu_usage'] - stats['precpu_stats']['system_cpu_usage']
            cpu_percent = (cpu_delta / system_delta) * len(stats['cpu_stats']['cpu_usage']['percpu_usage']) * 100.0
            return cpu_percent
        except:
            return 0.0
    
    def _calculate_memory_percent(self, stats):
        """Вычислить процент использования памяти"""
        try:
            memory_usage = stats['memory_stats']['usage']
            memory_limit = stats['memory_stats']['limit']
            return (memory_usage / memory_limit) * 100.0
        except:
            return 0.0
    
    async def start_container(self, container_name):
        """Запустить контейнер"""
        try:
            container = self.docker_client.containers.get(container_name)
            container.start()
            return True
        except Exception as e:
            print(f"Ошибка при запуске контейнера: {e}")
            return False
    
    async def stop_container(self, container_name):
        """Остановить контейнер"""
        try:
            container = self.docker_client.containers.get(container_name)
            container.stop()
            return True
        except Exception as e:
            print(f"Ошибка при остановке контейнера: {e}")
            return False
    
    async def restart_container(self, container_name):
        """Перезапустить контейнер"""
        try:
            container = self.docker_client.containers.get(container_name)
            container.restart()
            return True
        except Exception as e:
            print(f"Ошибка при перезапуске контейнера: {e}")
            return False
    
    async def get_container_logs(self, container_name, lines=20):
        """Получить логи контейнера"""
        try:
            container = self.docker_client.containers.get(container_name)
            logs = container.logs(tail=lines).decode('utf-8')
            return logs
        except Exception as e:
            print(f"Ошибка при получении логов: {e}")
            return f"Ошибка при получении логов: {e}"
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start"""
        # Опционально: проверка доступа
        # user_id = update.effective_user.id
        # if hasattr(self, 'allowed_users') and self.allowed_users and user_id not in self.allowed_users:
        #     await update.message.reply_text("❌ У вас нет доступа к этому боту.")
        #     return
        
        keyboard = [
            [InlineKeyboardButton("📋 Список контейнеров", callback_data="list")],
            [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
            [InlineKeyboardButton("🔐 Серверы (remote)", callback_data="ssh_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🐳 *Docker Bot*\n\nВыберите действие:",
            reply_markup=reply_markup,
        )
    
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка нажатий на кнопки"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "list":
            await self.show_containers(query)
        elif query.data == "stats":
            await self.show_stats(query)
        elif query.data == "back":
            await self.start_menu(query)
        elif query.data == "ssh_menu":
            await self.show_ssh_menu(query)
        elif query.data == "ssh_add":
            await self.start_add_ssh_server(query)
        elif query.data.startswith("ssh_connect_"):
            server_id = query.data.replace("ssh_connect_", "")
            await self.show_remote_containers(query, server_id)
        elif query.data.startswith("ssh_stats_"):
            server_id = query.data.replace("ssh_stats_", "")
            await self.show_remote_stats(query, server_id)
        elif query.data.startswith("ssh_delete_confirm_"):
            server_id = query.data.replace("ssh_delete_confirm_", "")
            await self.delete_server(query, server_id)
        elif query.data.startswith("ssh_delete_"):
            server_id = query.data.replace("ssh_delete_", "")
            await self.confirm_delete_server(query, server_id)
        elif query.data.startswith("sshc|"):
            parts = query.data.split("|")
            # Formats:
            # sshc|info|{server_id}|{enc_name}
            # sshc|action|{server_id}|{start|stop|restart}|{enc_name}
            # sshc|logs|{server_id}|{enc_name}
            if len(parts) >= 3:
                cmd = parts[1]
                if cmd == 'info' and len(parts) == 4:
                    await self.show_remote_container_info(query, parts[2], unquote(parts[3]))
                elif cmd == 'action' and len(parts) == 5:
                    await self.handle_remote_action(query, parts[2], parts[3], unquote(parts[4]))
                elif cmd == 'logs' and len(parts) == 4:
                    await self.show_remote_logs(query, parts[2], unquote(parts[3]))
        elif query.data.startswith("container_"):
            await self.show_container_info(query)
        elif query.data.startswith("action_"):
            await self.handle_action(query)
    
    async def start_menu(self, query):
        """Показать главное меню"""
        keyboard = [
            [InlineKeyboardButton("📋 Список контейнеров", callback_data="list")],
            [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
            [InlineKeyboardButton("🔐 Серверы (SSH)", callback_data="ssh_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "🐳 *Docker Bot*\n\nВыберите действие:",
            reply_markup=reply_markup,
        )
    
    async def show_containers(self, query):
        """Показать список контейнеров"""
        containers = await self.get_containers()
        
        if not containers:
            await query.edit_message_text("📋 Контейнеры не найдены")
            return
        
        message = "📋 <b>Список контейнеров:</b>\n\n"
        keyboard = []
        
        for container in containers:
            status_emoji = "🟢" if container['status'] == 'running' else "🔴"
            
            message += f"{status_emoji} <code>{html.escape(container['name'])}</code>\n"
            message += f"   Статус: {html.escape(container['status'])}\n"
            message += f"   Образ: {html.escape(container['image'])}\n\n"
            
            keyboard.append([
                InlineKeyboardButton(
                    f"{'⏹️' if container['status'] == 'running' else '▶️'} {container['name']}",
                    callback_data=f"container_{container['name']}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    async def show_ssh_menu(self, query):
        """Меню SSH серверов"""
        user_id = query.from_user.id
        user_servers = self.user_servers.get(user_id, [])
        env_servers = self.env_servers

        message = "🔐 *Серверы (SSH):*\n\n"
        keyboard = []

        if not env_servers and not user_servers:
            message += "Нет сохраненных серверов. Добавьте новый.\n\n"
        else:
            if env_servers:
                message += "Из окружения:\n"
                for idx, srv in enumerate(env_servers):
                    label = f"{srv['username']}@{srv['host']}"
                    keyboard.append([InlineKeyboardButton(f"📋 {label}", callback_data=f"ssh_connect_env_{idx}")])
                    keyboard.append([InlineKeyboardButton(f"📊 Статистика: {label}", callback_data=f"ssh_stats_env_{idx}")])
                message += "\n"
            if user_servers:
                message += "Ваши сервера:\n"
                for idx, srv in enumerate(user_servers):
                    label = f"{srv['username']}@{srv['host']}"
                    keyboard.append([InlineKeyboardButton(f"📋 {label}", callback_data=f"ssh_connect_user_{idx}")])
                    keyboard.append([InlineKeyboardButton(f"📊 Статистика: {label}", callback_data=f"ssh_stats_user_{idx}")])
                    keyboard.append([InlineKeyboardButton(f"🗑️ Удалить: {label}", callback_data=f"ssh_delete_user_{idx}")])

        keyboard.append([InlineKeyboardButton("➕ Добавить сервер", callback_data="ssh_add")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(message, reply_markup=reply_markup)

    async def confirm_delete_server(self, query, server_id: str):
        user_id = query.from_user.id
        scope, srv = self._resolve_server_by_id(server_id, user_id)
        if scope != 'user' or not srv:
            await query.edit_message_text("❌ Этот сервер нельзя удалить")
            return

        label = f"{srv['username']}@{srv['host']}"
        message = f"Удалить сервер {label}?"
        keyboard = [
            [InlineKeyboardButton("✅ Да, удалить", callback_data=f"ssh_delete_confirm_{server_id}")],
            [InlineKeyboardButton("❌ Отмена", callback_data="ssh_menu")]
        ]
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

    async def delete_server(self, query, server_id: str):
        user_id = query.from_user.id
        scope, srv = self._resolve_server_by_id(server_id, user_id)
        if scope != 'user' or not srv:
            await query.edit_message_text("❌ Этот сервер нельзя удалить")
            return
        servers = self.user_servers.get(user_id, [])
        idx = int(server_id.split('_', 1)[1])
        removed = servers.pop(idx)
        if not servers:
            self.user_servers.pop(user_id, None)

        label = f"{removed['username']}@{removed['host']}"
        await query.edit_message_text(f"✅ Сервер удален: {label}")
        # Показать обновленное меню
        await self.show_ssh_menu(query)

    async def start_add_ssh_server(self, query):
        """Запустить мастер добавления SSH сервера и установки ключа"""
        user_id = query.from_user.id
        self.user_states[user_id] = {
            'flow': 'add_server',
            'step': 'host',
            'temp': {}
        }
        await query.edit_message_text("Введите host (ip/домен) сервера:")

    async def text_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка текстового ввода для сценариев SSH"""
        user_id = update.effective_user.id
        state = self.user_states.get(user_id)
        if not state:
            return

        if state.get('flow') == 'add_server':
            if state.get('step') == 'host':
                state['temp']['host'] = update.message.text.strip()
                state['step'] = 'username'
                await update.message.reply_text("Введите имя пользователя (например, root):")
                return
            if state.get('step') == 'username':
                state['temp']['username'] = update.message.text.strip()
                state['step'] = 'password'
                await update.message.reply_text("Введите пароль пользователя (это разово, для установки ключа):")
                return
            if state.get('step') == 'password':
                # Берём пароль и удаляем сообщение пользователя из чата
                state['temp']['password'] = update.message.text.strip()
                try:
                    await update.message.delete()
                except Exception:
                    # Могут быть ограничения на удаление — просто игнорируем
                    pass
                host = state['temp']['host']
                username = state['temp']['username']
                password = state['temp']['password']

                await update.message.reply_text("Пробую установить ключ и сохранить сервер...")
                try:
                    server_entry = await self._install_key_and_save_server(user_id, host, username, password)
                except Exception as e:
                    self.user_states.pop(user_id, None)
                    await update.message.reply_text(f"❌ Не удалось установить ключ: {e}")
                    return

                self.user_states.pop(user_id, None)
                label = f"{server_entry['username']}@{server_entry['host']}"
                await update.message.reply_text(f"✅ Готово. Сервер сохранен: {label}")
                # Показать меню SSH
                keyboard = [
                    [InlineKeyboardButton("📋 Открыть список серверов", callback_data="ssh_menu")]
                ]
                await update.message.reply_text("Что дальше?", reply_markup=InlineKeyboardMarkup(keyboard))
                return

    async def _install_key_and_save_server(self, user_id: int, host: str, username: str, password: str):
        """Сгенерировать ключ, установить на сервер через пароль, сохранить запись"""
        private_key_str, public_key_str = self._generate_ssh_keypair(comment=f"{username}@dockerbot")

        # Установим ключ на сервер, используя пароль
        self._ssh_copy_id(host, username, password, public_key_str)

        # Сохраняем сервер
        server_entry = {
            'host': host,
            'username': username,
            'private_key': private_key_str,
            'public_key': public_key_str
        }
        self.user_servers.setdefault(user_id, []).append(server_entry)
        return server_entry

    def _generate_ssh_keypair(self, comment: str = "dockerbot"):
        key = paramiko.RSAKey.generate(2048)
        private_io = io.StringIO()
        key.write_private_key(private_io)
        private_key_str = private_io.getvalue()
        public_key_str = f"{key.get_name()} {key.get_base64()} {comment}"
        return private_key_str, public_key_str

    def _ssh_copy_id(self, host: str, username: str, password: str, public_key: str):
        """Аналог ssh-copy-id: добавить ключ в authorized_keys"""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=host, username=username, password=password, timeout=20)
        try:
            commands = [
                "mkdir -p ~/.ssh",
                "chmod 700 ~/.ssh",
                "touch ~/.ssh/authorized_keys",
                "chmod 600 ~/.ssh/authorized_keys",
                # Добавляем ключ, если его еще нет
                f"grep -qxF '{public_key}' ~/.ssh/authorized_keys || echo '{public_key}' >> ~/.ssh/authorized_keys"
            ]
            for cmd in commands:
                self._ssh_exec_client(ssh, cmd)
        finally:
            ssh.close()

    def _build_pkey(self, private_key_str: str):
        return paramiko.RSAKey.from_private_key(io.StringIO(private_key_str))

    def _ssh_exec(self, host: str, username: str, private_key_str: str, command: str, timeout: int = 20):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        pkey = self._build_pkey(private_key_str)
        ssh.connect(hostname=host, username=username, pkey=pkey, timeout=timeout)
        try:
            return self._ssh_exec_client(ssh, command, timeout)
        finally:
            ssh.close()

    def _ssh_exec_client(self, ssh: paramiko.SSHClient, command: str, timeout: int = 20):
        stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
        out = stdout.read().decode('utf-8', errors='ignore').strip()
        err = stderr.read().decode('utf-8', errors='ignore').strip()
        if err and not out:
            return err
        return out

    async def show_remote_containers(self, query, server_id: str):
        user_id = query.from_user.id
        scope, srv = self._resolve_server_by_id(server_id, user_id)
        if not srv:
            await query.edit_message_text("❌ Сервер не найден")
            return

        output = self._ssh_exec(
            srv['host'], srv['username'], srv['private_key'],
            "docker ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}'"
        )

        lines = [l for l in output.split('\n') if l.strip()]
        if not lines:
            await query.edit_message_text("📋 Контейнеры не найдены (удаленно)")
            return

        message = "📋 <b>Список контейнеров (удаленно):</b>\n\n"
        keyboard = []
        for line in lines:
            try:
                name, status, image = line.split('|', 2)
            except ValueError:
                continue
            status_emoji = "🟢" if status.lower().startswith('up') else "🔴"
            message += f"{status_emoji} <code>{html.escape(name)}</code>\n"
            message += f"   Статус: {html.escape(status)}\n"
            message += f"   Образ: {html.escape(image)}\n\n"
            enc = quote(name, safe='')
            # Кнопка подробностей/управления
            keyboard.append([
                InlineKeyboardButton(
                    f"{'⏹️' if status_emoji=='🟢' else '▶️'} {name}",
                    callback_data=f"sshc|info|{server_id}|{enc}"
                )
            ])

        keyboard.append([InlineKeyboardButton("📊 Статистика", callback_data=f"ssh_stats_{server_id}")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="ssh_menu")])
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

    async def show_remote_stats(self, query, server_id: str):
        user_id = query.from_user.id
        scope, srv = self._resolve_server_by_id(server_id, user_id)
        if not srv:
            await query.edit_message_text("❌ Сервер не найден")
            return

        output = self._ssh_exec(
            srv['host'], srv['username'], srv['private_key'],
            "docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}'"
        )
        lines = [l for l in output.split('\n') if l.strip()]
        if not lines:
            await query.edit_message_text("Нет запущенных контейнеров (удаленно)")
            return

        message = "📊 <b>Статистика сервера (удаленно):</b>\n\n"
        for line in lines:
            try:
                name, cpu, mem = line.split('|', 2)
            except ValueError:
                continue
            message += f"🟢 {html.escape(name)}\n"
            message += f"   CPU: {html.escape(cpu)}\n"
            message += f"   Память: {html.escape(mem)}\n\n"

        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="ssh_menu")]]
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

    async def show_remote_container_info(self, query, server_id: str, container_name: str):
        user_id = query.from_user.id
        scope, srv = self._resolve_server_by_id(server_id, user_id)
        if not srv:
            await query.edit_message_text("❌ Сервер не найден")
            return

        # Получим статус, образ
        info_line = self._ssh_exec(
            srv['host'], srv['username'], srv['private_key'],
            f"docker ps -a --filter name=^/{container_name}$ --format '{{{{.Status}}}}|{{{{.Image}}}}'"
        ).strip()
        status = "unknown"
        image = ""
        if info_line and '|' in info_line:
            status, image = info_line.split('|', 1)

        message = f"🐳 <b>{html.escape(container_name)}</b>\n\n"
        message += f"Статус: {html.escape(status)}\n"
        message += f"Образ: {html.escape(image)}\n\n"

        enc = quote(container_name, safe='')
        keyboard = []
        if status.lower().startswith('up'):
            keyboard.append([InlineKeyboardButton("⏹️ Остановить", callback_data=f"sshc|action|{server_id}|stop|{enc}")])
            keyboard.append([InlineKeyboardButton("🔄 Перезапустить", callback_data=f"sshc|action|{server_id}|restart|{enc}")])
        else:
            keyboard.append([InlineKeyboardButton("▶️ Запустить", callback_data=f"sshc|action|{server_id}|start|{enc}")])
        keyboard.append([InlineKeyboardButton("📝 Логи", callback_data=f"sshc|logs|{server_id}|{enc}")])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data=f"ssh_connect_{server_id}")])

        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

    async def handle_remote_action(self, query, server_id: str, action: str, container_name: str):
        user_id = query.from_user.id
        scope, srv = self._resolve_server_by_id(server_id, user_id)
        if not srv:
            await query.edit_message_text("❌ Сервер не найден")
            return
        cmd = None
        if action == 'start':
            cmd = f"docker start {container_name}"
        elif action == 'stop':
            cmd = f"docker stop {container_name}"
        elif action == 'restart':
            cmd = f"docker restart {container_name}"
        else:
            await query.edit_message_text("❌ Неизвестное действие")
            return
        out = self._ssh_exec(srv['host'], srv['username'], srv['private_key'], cmd)
        del out
        # Обновим карточку контейнера
        await self.show_remote_container_info(query, server_id, container_name)

    async def show_remote_logs(self, query, server_id: str, container_name: str):
        user_id = query.from_user.id
        scope, srv = self._resolve_server_by_id(server_id, user_id)
        if not srv:
            await query.edit_message_text("❌ Сервер не найден")
            return
        logs = self._ssh_exec(srv['host'], srv['username'], srv['private_key'], f"docker logs --tail 50 {container_name}")
        if len(logs) > 3000:
            logs = logs[-3000:]
        message = f"📝 <b>Логи {html.escape(container_name)} (удаленно):</b>\n\n<pre>{html.escape(logs)}</pre>"
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data=f"sshc|info|{server_id}|{quote(container_name, safe='')}")]]
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

    def _load_env_servers(self):
        # Только парольные сервера: SSH_SERVERS_PWD_JSON
        raw_pwd = os.getenv('SSH_SERVERS_PWD_JSON', '')
        if raw_pwd is None:
            raw_pwd = ''
        raw_pwd = raw_pwd.strip()
        print(f"SSH_SERVERS_PWD_JSON present={bool(raw_pwd)} len={len(raw_pwd) if raw_pwd else 0}")
        pwd_based = []
        if raw_pwd:
            try:
                data_pwd = json.loads(raw_pwd)
                print(f"SSH_SERVERS_PWD_JSON parsed, type={type(data_pwd).__name__}")
                if isinstance(data_pwd, list):
                    print(f"SSH_SERVERS_PWD_JSON list size={len(data_pwd)}")
                    for idx, item in enumerate(data_pwd):
                        if not isinstance(item, dict):
                            print(f"SSH_SERVERS_PWD_JSON[{idx}] skipped: not a dict")
                            continue
                        host = item.get('host')
                        username = item.get('username')
                        password = item.get('password')
                        if not host or not username or not password:
                            print(f"SSH_SERVERS_PWD_JSON[{idx}] missing required fields")
                            continue
                        try:
                            entry = self._install_key_for_env(host, username, password)
                            pwd_based.append(entry)
                        except Exception as e:
                            print(f"SSH_SERVERS_PWD_JSON[{idx}] install failed: {e}")
                            continue
            except Exception as e:
                print(f"SSH_SERVERS_PWD_JSON json error: {e}")

        return pwd_based

    def _install_key_for_env(self, host: str, username: str, password: str):
        private_key_str, public_key_str = self._generate_ssh_keypair(comment=f"{username}@dockerbot-env")
        self._ssh_copy_id(host, username, password, public_key_str)
        return {
            'host': host,
            'username': username,
            'private_key': private_key_str,
            'public_key': public_key_str
        }

    def _resolve_server_by_id(self, server_id: str, user_id: int):
        # server_id может быть вида: "env_0" или "user_1" или старый int (совместимость)
        if server_id.isdigit():
            servers = self.user_servers.get(user_id, [])
            try:
                idx = int(server_id)
                return 'user', servers[idx]
            except Exception:
                return None, None
        if '_' in server_id:
            scope, idx_str = server_id.split('_', 1)
            try:
                idx = int(idx_str)
            except Exception:
                return None, None
            if scope == 'env':
                try:
                    return 'env', self.env_servers[idx]
                except Exception:
                    return None, None
            if scope == 'user':
                servers = self.user_servers.get(user_id, [])
                try:
                    return 'user', servers[idx]
                except Exception:
                    return None, None
        return None, None
    
    async def show_container_info(self, query):
        """Показать информацию о контейнере"""
        container_name = query.data.split("_")[1]
        
        try:
            container = self.docker_client.containers.get(container_name)
            status = container.status
            
            message = f"🐳 <b>{html.escape(container_name)}</b>\n\n"
            message += f"Статус: {html.escape(status)}\n"
            message += f"Образ: {html.escape(container.image.tags[0] if container.image.tags else container.image.short_id)}\n\n"
            
            keyboard = []
            
            if status == 'running':
                keyboard.append([InlineKeyboardButton("⏹️ Остановить", callback_data=f"action_stop_{container_name}")])
                keyboard.append([InlineKeyboardButton("🔄 Перезапустить", callback_data=f"action_restart_{container_name}")])
            else:
                keyboard.append([InlineKeyboardButton("▶️ Запустить", callback_data=f"action_start_{container_name}")])
            
            keyboard.append([InlineKeyboardButton("📝 Логи", callback_data=f"action_logs_{container_name}")])
            keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="list")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(message, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка при получении информации о контейнере: {e}")
    
    async def handle_action(self, query):
        """Обработка действий с контейнерами"""
        data = query.data.split("_")
        action = data[1]
        container_name = "_".join(data[2:])
        
        if action == "start":
            success = await self.start_container(container_name)
            if success:
                await query.edit_message_text(f"✅ Контейнер {container_name} запущен")
            else:
                await query.edit_message_text(f"❌ Ошибка при запуске контейнера {container_name}")
        elif action == "stop":
            success = await self.stop_container(container_name)
            if success:
                await query.edit_message_text(f"⏹️ Контейнер {container_name} остановлен")
            else:
                await query.edit_message_text(f"❌ Ошибка при остановке контейнера {container_name}")
        elif action == "restart":
            success = await self.restart_container(container_name)
            if success:
                await query.edit_message_text(f"🔄 Контейнер {container_name} перезапущен")
            else:
                await query.edit_message_text(f"❌ Ошибка при перезапуске контейнера {container_name}")
        elif action == "logs":
            logs = await self.get_container_logs(container_name, 20)
            if len(logs) > 3000:
                logs = logs[-3000:] + "\n\n... (показаны последние 20 строк)"

            message = f"📝 <b>Логи {html.escape(container_name)}:</b>\n\n<pre>{html.escape(logs)}</pre>"
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data=f"container_{container_name}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(message, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    
    async def show_stats(self, query):
        """Показать статистику"""
        stats_text = await self.get_container_stats()
        
        # Подсчет контейнеров
        containers = await self.get_containers()
        total_containers = len(containers)
        running_containers = len([c for c in containers if c['status'] == 'running'])
        
        message = "📊 *Статистика сервера:*\n\n"
        message += f"🌐 Контейнеры: {running_containers}/{total_containers}\n\n"
        message += stats_text
        
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, reply_markup=reply_markup)
    
    def run(self):
        """Запуск бота"""
        defaults = Defaults(parse_mode=ParseMode.MARKDOWN)
        application = Application.builder().token(self.bot_token).defaults(defaults).build()
        
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CallbackQueryHandler(self.button_handler))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_handler))
        
        print("Бот запущен...")
        application.run_polling()

if __name__ == "__main__":
    bot = DockerBot()
    bot.run()
