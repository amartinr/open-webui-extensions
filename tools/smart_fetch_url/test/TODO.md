# TODO — Content-Type Detection

## 🔴 Bloqueantes

- [ ] **T2 (blog article)**: encontrar una URL de artículo real con `og:type="article"`
      que responda 200. Intentar con `https://tonsky.me/blog/thermocline/`
      u otros blogs conocidos.

## 🟡 Mejoras en heurísticas

- [ ] **Cobertura de feeds con HTML de tablas**: Hacker News, Lobsters y sitios
      similares se clasifican como `"unknown"` porque su layout usa `<tr class="athing">`
      en vez de clases CSS modernas. Considerar añadir señal S11 para detectar
      filas repetidas con clase tipo `athing`, `row`, etc.
      *Nota: trafilatura ya extrae bien estas páginas, así que esto es cosmetico.*

- [ ] **Ajustar pesos/umbrales** para que 5 `.post` + paginación = `"feed"` sea
      más robusto. Actualmente funciona pero apenas roza el umbral.

- [ ] **Señal para feeds con `<table>` + clases específicas** (Reddit old-style).
      Investigar qué clases usan los foros/agregadores con tablas.

## 🟢 Tests

- [ ] **Test de regresión para `format="raw"`**: verificar que `smart_fetch_url()`
      con `format="raw"` nunca pase por `_detect_content_type()`.

- [ ] **Test de no-regresión para `_try_alternate_fallback()`**: confirmar que
      el fallback por alternate no se activa en feeds (word_count ≥ 30).

- [ ] **Test de `_format_output()`** con cada formato (markdown, html, txt, json, raw)
      para verificar que el early return de feed genera dicts compatibles.

- [ ] **Test de batch_fetch_urls** con mezcla de feeds y artículos.

## 📦 CI / Tooling

- [ ] Añadir `requirements-test.txt` o `test/requirements.txt` con dependencias
      de test (pytest si se adopta, o simplemente documentar que se usa unittest).

- [ ] Crear script `run_tests.sh` que active el venv y ejecute los tres módulos
      de test en secuencia.
