# SubWords — Unknown Words from Subtitles

Desktop‑утилита на Python/Tkinter для анализа субтитров и выделения неизвестных слов.
Поддерживает SRT/VTT и извлечение субтитров из видео через FFmpeg. Есть лемматизация (spaCy) и перевод (deep‑translator).

## Возможности
- Подсчет уникальных токенов и неизвестных слов;
- Обновление счетчиков в реальном времени при добавлении слов в словари;
- Импорт/экспорт слов (CSV);
- Извлечение субтитров из MKV/MP4 (ffmpeg/ffprobe);
- Сохранение результатов анализа;
- Готовая сборка в EXE через PyInstaller.

> Название и описание можно адаптировать под ваш бренд.

## Быстрый старт (разработчикам)
```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
pip install -U pip
pip install -r requirements.txt
python -m spacy download en_core_web_sm
python win_unknown_words_app.py
```

## Сборка EXE (локально)
```bash
pyinstaller win_unknown_words_app.py ^
  --onefile ^
  --noconsole ^
  --name SubWords ^
  --icon ".\SW.ico" ^
  --collect-all spacy ^
  --collect-all en_core_web_sm ^
  --add-binary ".\ffmpeg.exe;." ^
  --add-binary ".\ffprobe.exe;."
```
> Для сборки нужны `ffmpeg.exe` и `ffprobe.exe` в корне проекта (см. workflow ниже — он сам скачает их на CI).

## CI/CD
Готов GitHub Actions workflow для автоматической сборки EXE на Windows и загрузки в Releases при создании git‑тега.
См. файл `.github/workflows/windows-build.yml`.

## Донаты
Варианты для жителей России приведены в [DONATIONS.md](DONATIONS.md). В приложении есть пункт меню «Поддержать проект», который открывает ссылку на вашу страницу донатов.

## Лицензия
Код распространяется по лицензии MIT (см. `LICENSE`). Обратите внимание на лицензии FFmpeg и моделей spaCy.
