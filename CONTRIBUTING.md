# Участие в Renisin

Проект распространяется по двойной лицензии. Выражение SPDX:

    SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

Открытые условия — AGPL-3.0-only, текст в `LICENSES/AGPL-3.0-only.txt`.
Коммерческие условия — отдельный договор, идентификатор `LicenseRef-Commercial` в `LICENSES/LicenseRef-Commercial.txt`.
Краткое уведомление — в `LICENSE`.


## Подпись CLA

Отправка pull request требует подписания Broad CLA (`CLA.md`).

Без действующей подписи CLA pull request не принимается и не вливается в `master`.
Подпись ставит учётная запись GitHub, с которой подаётся вклад. Она покрывает текущий и следующие вклады этой учётной записи, пока текст `CLA.md` не изменился. После изменения текста нужна новая подпись.

CLA даёт правообладателю бессрочную безотзывную неисключительную лицензию на вклад: использование, изменение, распространение, сублицензирование и перелицензирование, включая коммерческие условия, а также патентную лицензию. Участник подтверждает, что вправе это предоставить. Исключительное право на вклад у участника остаётся.

Порядок проверки подписи описан в `docs/dual-licensing.md`. Пока эта проверка не включена в репозитории, внешние pull request всё равно не принимаются без подписанного CLA.


## Заголовок в исходных файлах

В новый или существенно изменённый исходный файл добавьте заголовок:

```
Copyright (c) 2026 <Владелец>. Dual-licensed under AGPL-3.0 and Commercial License.
SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial
```

Для Python:

```python
# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial
```

Готовый шаблон: `docs/copyright-header.txt`.

Если во вклад входит код третьих лиц, укажите в pull request файл, лицензию и источник. CLA перелицензирует только то, на что у участника есть права.
