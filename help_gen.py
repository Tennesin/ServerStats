"""Генератор справки: создаёт текстовый файл «Команды бота.txt» со всеми командами и инструкциями.

Запуск:  python help_gen.py
Файл появляется рядом со скриптом. Бот для этого запускать не нужно.

Динамические части (пресеты периодов, варианты значений, папки, даты) берутся из config.py.
Описания команд и параметров записаны ниже: при изменении cli.py поправьте их здесь.
"""

import os
import textwrap

from config import (
    ALLDAY_DIR, DATE_FMT, DETAIL_LABELS, GUILD_START_DATES, MIN_START_DATE, PERIOD_PRESETS,
    REACTION_LABELS, REACTION_REFRESH_DAYS, SORT_LABELS, STATS_DIR, TZ_NAME,
)

OUT_NAME = "Команды бота.txt"
WIDTH = 96

# (имя без "--", значение, описание). Значение пустое — параметр-флаг.
STATS_OPTIONS = [
    ("period", "ПЕРИОД", "Готовый период (список пресетов ниже)."),
    ("last", "45d", "Сокращение для пресета last: 45d = last45d. Буквы: d — дни, w — недели, "
                    "m — месяцы, y — годы. Нельзя вместе с --period."),
    ("start", "дд.мм.гггг", "Дата начала (вместо пресета)."),
    ("end", "дд.мм.гггг", "Дата конца включительно (вместо пресета). Не задана — берётся вчера. "
                          "Позже вчерашнего дня быть не может."),
    ("compare", "", "Сравнить с предыдущим периодом такой же длины. Читается вдвое больше данных. "
                    "Если предыдущий период начался бы раньше запуска сервера — сравнение отключается."),
    ("exclude", '"чат1,чат2"', "Доп. чаты для пропуска только в этом запуске: точные названия "
                               "или ID через запятую (регистр не важен)."),
    ("detail", "ВАРИАНТ", "Детализация листа «Матрица» (варианты ниже)."),
    ("sort", "ВАРИАНТ", "Порядок пользователей в отчёте (варианты ниже)."),
    ("reactions", "ВАРИАНТ", "Режим сбора реакций (варианты ниже). По умолчанию — counts."),
    ("nosync", "", "Не обращаться к Discord: построить отчёт только из базы (быстро, по уже собранным данным)."),
    ("live", "", "Читать Discord напрямую, не используя базу (медленно, как в старых версиях). "
                 "Несовместимо с --nosync."),
]

def wrap(text: str, indent: int) -> str:
    return textwrap.fill(text, width=WIDTH, initial_indent=" " * indent,
                         subsequent_indent=" " * indent)

def options_block() -> list:
    lines = []
    for name, value, desc in STATS_OPTIONS:
        head = f"--{name} {value}".rstrip()
        lines.append(f"  {head}")
        lines.append(wrap(desc, 6))
    return lines

def choices_block(title: str, mapping: dict, default: str = "") -> list:
    lines = [f"  {title}:"]
    for key, label in mapping.items():
        mark = "  (по умолчанию)" if key == default else ""
        lines.append(f"      {key:<8} — {label}{mark}")
    return lines

def build_text() -> str:
    out = []
    add = out.append

    def title(text: str):
        add("")
        add("=" * WIDTH)
        add(text.upper())
        add("=" * WIDTH)

    add("СПРАВКА ПО БОТУ СТАТИСТИКИ")
    add("Файл создан автоматически (help_gen.py); при изменении команд создайте его заново.")

    title("1. Как пользоваться")
    for p in (
        "Запустите бота: python main.py (в PyCharm — кнопка Run).",
        "Команды вводятся в консоль запущенного бота (вкладка Run внизу окна PyCharm): "
        "щёлкните мышью в консоли, наберите команду и нажмите Enter. Все команды начинаются с дефиса.",
        "Слеш-команд в Discord больше нет: бот работает только через терминал.",
        "Пока идёт сбор, можно вводить -cancel. Новый -stats или -sync во время сбора запустить нельзя.",
        "Закончив, остановите бота (Ctrl+C или кнопка Stop).",
    ):
        add(wrap("• " + p, 2).replace("  •", "•", 1) if False else wrap("• " + p, 2))

    title("2. Команды терминала")
    add("  -servers")
    add(wrap("Показать серверы, где есть бот, и их ID.", 6))
    add("")
    add("  -stats ID1 ID2 ... [параметры]")
    add(wrap("Собрать статистику по указанным серверам и сохранить Excel-отчёт каждого. "
             "ID можно разделять пробелами или запятыми.", 6))
    add("")
    add("  -stats all [параметры]")
    add(wrap("То же по всем серверам, где есть бот.", 6))
    add(wrap("Если серверов обработано больше одного, дополнительно создаётся файл «Суммарная статистика.xlsx».", 6))
    add("")
    add("  -sync ID1 ID2 ... [--reactions ВАРИАНТ]")
    add("  -sync all [--reactions ВАРИАНТ]")
    add(wrap("Обновить только базу данных (сообщения, реакции, журнал аудита), без отчётов. "
             "Подходит, чтобы заранее дочитать новое, а потом строить отчёты быстро через --nosync.", 6))
    add("")
    add("  -cancel")
    add(wrap("Остановить текущий сбор. Уже собранное сохранится, отчёт будет помечен как неполный.", 6))

    title("3. Параметры команды -stats")
    add(wrap("Во всех режимах учитываются только ЗАВЕРШЁННЫЕ сутки (по часовому поясу "
             f"{TZ_NAME}): последний день периода — не позже вчерашнего.", 2))
    add(wrap("Если не указан ни пресет, ни даты, отчёт строится за всю историю.", 2))
    add(wrap("Параметры пишутся после ID серверов (или после all), в любом порядке.", 2))
    add("")
    out.extend(options_block())
    add("")
    out.extend(choices_block("Варианты --detail", DETAIL_LABELS, "auto"))
    add("")
    out.extend(choices_block("Варианты --sort", SORT_LABELS, "count"))
    add("")
    out.extend(choices_block("Варианты --reactions", REACTION_LABELS, "counts"))
    add("")
    add(wrap("Пресеты для --period (можно писать и в свободной форме: lastNd / lastNw / lastNm / lastNy, "
             "например last45d, last2w, last8m):", 2))
    for key, label in PERIOD_PRESETS.items():
        add(f"      {key:<11} — {label}")

    title("4. Как работает база данных")
    for p in (
        "Бот хранит прочитанную историю в файле stats.db рядом с программой. По умолчанию -stats сначала "
        "дочитывает из Discord только новое, затем строит отчёт из базы. Поэтому повторные запуски быстрые.",
        f"Реакции у сообщений младше {REACTION_REFRESH_DAYS} дней перечитываются при каждой синхронизации, "
        "потому что они меняются.",
        "Режим реакций full (кто именно ставил) работает медленно и нужен для рейтингов «кто кому ставит». "
        "Если в базе нет списков людей, режим full автоматически заменяется на counts, и в отчёте появится примечание.",
        "Смена TIMEZONE при существующей базе запрещена: даты хранятся по локальному времени.",
        "Очистка данных: python reset_data.py (полная очистка сообщений, реакций и эмодзи; журнал аудита "
        "не затрагивается) или python reset_data.py marks (только сброс «водяных знаков»: история "
        "перечитается заново поверх существующих данных). Перед очисткой автоматически делается резервная копия.",
    ):
        add(wrap("• " + p, 2))

    title("5. Где лежат отчёты")
    base = STATS_DIR or "output (рядом с программой)"
    add(wrap(f"Обычные отчёты: {base}\\ГГГГ-ММ\\ДД-ММ - ДД-ММ\\ — папка с xlsx-файлом каждого сервера "
             "(«Название (ID).xlsx») и, при нескольких серверах, «Суммарная статистика.xlsx».", 2))
    add(wrap(f"Отчёты за всю историю: {ALLDAY_DIR or 'НЕ ЗАДАНО — укажите ALLDAY_DIR в файле .env'}"
             "\\ДД-ММ-ГГГГ - ДД-ММ-ГГГГ\\", 2))
    if not ALLDAY_DIR:
        add(wrap("ВНИМАНИЕ: без ALLDAY_DIR команда -stats без периода и дат (вся история) завершится ошибкой.", 2))
    add(wrap("Если папка с таким именем уже есть, к новой добавляется номер: «(2)», «(3)» и т. д.", 2))

    title("6. Границы истории")
    add(wrap(f"Раньше {MIN_START_DATE.strftime(DATE_FMT)} бот не читает ни один сервер. "
             "Для каждого сервера период дополнительно подрезается под дату его официального запуска:", 2))
    for gid, d in GUILD_START_DATES.items():
        add(f"      {gid} — с {d.strftime(DATE_FMT)}")
    add(wrap("Если выбранный период целиком раньше запуска сервера, сервер пропускается.", 2))

    title("7. Примеры")
    for cmd, note in (
        ("-servers", "посмотреть ID серверов"),
        ("-stats all", "вся история по всем серверам"),
        ("-stats 123456789012345678 --last 30d", "последние 30 дней одного сервера"),
        ("-stats 111111111111111111 222222222222222222 --period prev-month --compare",
         "прошлый месяц по двум серверам со сравнением"),
        ("-stats all --start 01.10.2026 --end 15.10.2026 --detail weeks", "свои даты, матрица по неделям"),
        ("-stats all --period last7d --reactions full", "неделя с полным сбором реакций (долго)"),
        ('-stats all --last 14d --exclude "флуд,мемы"', "разово пропустить два чата"),
        ("-sync all", "только обновить базу"),
        ("-stats all --period prev-week --nosync", "отчёт строго из базы, без обращения к Discord"),
    ):
        add(f"  {cmd}")
        add(f"      → {note}")

    title("8. Анализатор отчётов (analyzer/analyze.py)")
    add(wrap("Отдельная программа: читает готовые xlsx-отчёты бота и строит HTML-отчёты по серверам "
             "и пользователям (с индексом СЗП) и сводный xlsx. Бот при этом запускать не нужно.", 2))
    add("")
    add('  python analyze.py "путь_к_папке_с_xlsx" [параметры]')
    add(wrap("Запускать из папки analyzer. Путь можно указать на один файл, на папку со сборником, "
             "на папку «ГГГГ-ММ» или на корень output: из вложенных сборников берётся самый свежий. "
             "Результат сохраняется в папку analysis рядом с xlsx.", 6))
    add("")
    for opt, desc in (
        ("--top N", "сколько пользователей показать в топе в терминале (0 — не показывать)"),
        ("--out ПАПКА", "куда сохранять отчёты (по умолчанию — analysis рядом с xlsx)"),
        ("--no-reports", "только сводка в терминале, без HTML и xlsx"),
        ("--no-avatars", "не скачивать аватарки и картинки эмодзи (работа без интернета)"),
        ("--top-n N", "сколько лучших по СЗП получают индивидуальный отчёт "
                      "(по умолчанию — SELECT_TOP_N в analyzer/settings.py)"),
        ("--min-szp X", "и все, у кого СЗП не ниже X (по умолчанию — SELECT_MIN_SZP)"),
        ("--max-individual N", "предел числа индивидуальных отчётов на файл (по умолчанию — MAX_INDIVIDUAL)"),
    ):
        add(f"  {opt}")
        add(wrap(desc, 6))

    add("")
    return "\n".join(out)

def check_against_cli() -> list:
    """Сверка списка параметров с реальным cli.py. Возвращает список предупреждений."""
    try:
        import cli
    except Exception as e:                                   # noqa: BLE001
        return [f"Не удалось сверить параметры с cli.py: {e}"]
    documented = {name for name, _v, _d in STATS_OPTIONS}
    real = set(cli.VALUE_OPTS) | set(cli.FLAG_OPTS)
    warns = []
    for name in sorted(real - documented):
        warns.append(f"В cli.py есть параметр --{name}, которого нет в справке: добавьте его в STATS_OPTIONS.")
    for name in sorted(documented - real):
        warns.append(f"В справке описан параметр --{name}, но в cli.py его больше нет: удалите из STATS_OPTIONS.")
    return warns

def main():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUT_NAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_text())
    print(f"Справка сохранена: {path}")
    for w in check_against_cli():
        print("⚠", w)

if __name__ == "__main__":
    main()