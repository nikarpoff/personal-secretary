"""
Разовое согласие Google OAuth: получает refresh-токен календаря и сохраняет его
в формате authorized_user - этот файл и указывается в GOOGLE_CREDENTIALS_FILE.

Нужен OAuth-клиент типа «Desktop app» из Google Cloud Console (JSON клиента).
Запуск там, где есть браузер:

    uv run scripts/authorize_google.py --client secrets/client_secret.json --out secrets/google.json

На сервере без браузера пробросьте порт и откройте напечатанную ссылку у себя:

    ssh -L 8765:localhost:8765 <сервер>
    uv run scripts/authorize_google.py --client secrets/client_secret.json --no-browser --port 8765
"""

import argparse
import json
import sys

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

# Скрипт запускается из корня репозитория, а пакет app лежит рядом
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import CALENDAR_SETTINGS  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client", type=Path, required=True, help="JSON OAuth-клиента «Desktop app» из Google Cloud Console")
    parser.add_argument("--out", type=Path, default=Path("secrets/google.json"), help="куда сохранить токен (по умолчанию %(default)s)")
    parser.add_argument("--port", type=int, default=8765, help="локальный порт для редиректа Google (по умолчанию %(default)s)")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер, только напечатать ссылку")
    args = parser.parse_args(argv)

    flow = InstalledAppFlow.from_client_secrets_file(str(args.client), scopes=list(CALENDAR_SETTINGS.SCOPES))
    # offline + consent: без них Google не выдаёт refresh-токен при повторном входе
    credentials = flow.run_local_server(
        port=args.port,
        open_browser=not args.no_browser,
        access_type="offline",
        prompt="consent",
    )
    if not credentials.refresh_token:
        print("Google не выдал refresh-токен: отзовите доступ приложения в аккаунте и повторите", file=sys.stderr)
        return 1

    # load_credentials_from_file определяет вид учётных данных по полю type
    info = {**json.loads(credentials.to_json()), "type": "authorized_user"}
    info.pop("expiry", None)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(info, indent=2))
    args.out.chmod(0o600)
    print(f"Готово: {args.out}. Укажите его в GOOGLE_CREDENTIALS_FILE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
