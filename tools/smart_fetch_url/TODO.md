# TODO — smart_fetch_url refactoring

Branch: `refactor/smart-fetch`
Base: review feedback on fitness for the Open WebUI harness.

---

## P0 — Bugs / resource leaks

- [x] **Close `_httpx_client` properly**
  Removed the shared cached client (`_get_httpx_client()`) and switched
  `_fetch_with_httpx()` to use `async with httpx.AsyncClient()` per
  request.  Since httpx is only a fallback path, the overhead of creating
  a client per call is negligible.

- [x] **`pypdf` not needed in requirements** — it is already a transitive
  dependency of Open WebUI itself (used for document processing/RAG),
  so it is always available at runtime.

---

## P0·UX — User-facing issues in the harness

- [ ] **UI freezes during fetch — no progress feedback**
  Open WebUI pauses token streaming while a tool runs.  The user sees a
  frozen screen for the entire fetch + extraction time.  Currently the
  tool only emits `"source"` events at the very end.

  Consequences:
  - Single slow URL (~15s timeout): user thinks the chat is broken
  - Batch of 50 URLs: 30–90s of silence, no indication of progress
  - `batch_fetch_urls` explicitly passes `__event_emitter__=None`,
    so per-item progress is suppressed entirely

  **Technical constraints (from Open WebUI docs):**

  1. **Must use `"type": "status"`** — this is the only real-time
     feedback type that works in **Native Mode** (the only supported
     mode).  Types like `"message"`, `"chat:message:delta"` and
     `"replace"` are **BROKEN** in Native Mode — they get overwritten
     by native completion snapshots.

  2. **Payload format** (works identically in Default and Native modes):
     ```python
     {
         "type": "status",
         "data": {
             "description": "Human-readable text",
             "done": False,   # False = shimmer animation
             "hidden": False,  # True = saved to history, not shown
         }
     }
     ```

  3. **Always emit a final `done: True`** — without it the shimmer
     animation stays forever, making the tool look stuck even after
     completion.

  4. **`"source"` / `"citation"` events work in both modes** — the
     tool already uses these correctly for the citations list.

  **Implementation plan** — 5 cambios numerados, por orden de implementación:

  ---
  ### ✅ Cambio 1 — Helper `_emit_status()`

  **Archivo**: `smart_fetch_url.py`
  **Insertar**: junto a `_emit_sources` (~l. 1406), antes o después

  Crear un método auxiliar que centralice el formato del evento status:

  ```python
  async def _emit_status(self, emitter, description, done=False):
      if emitter is None:
          return
      try:
          await emitter({
              "type": "status",
              "data": {"description": description, "done": done},
          })
      except Exception:
          pass  # best-effort
  ```

  **Verificación**: `Tools()._emit_status(None, "test")` no lanza error.
  **Riesgo**: Ninguno — método nuevo sin callers.

  ---
  ### Cambio 2 — Status events en `smart_fetch_url()`

  **Archivo**: `smart_fetch_url.py`
  **Puntos de inserción**: entre l. 207 y l. 363 (cuerpo del try)

  Resolver el Valve `verbose` al inicio (junto a los otros valves, l. 207):
  ```python
  uv = self._get_user_valves(__user__)
  max_chars = ...
  timeout_ms = ...
  browser = ...
  verbose = (uv.verbose if uv else None) or self.valves.verbose  # NUEVA
  ```

  Insertar llamadas a `_emit_status` en estos puntos:

  | # | Tras línea | Evento | `done` | Condición |
  |---|-----------|--------|--------|-----------|
  | A | l. 215 (try) | `"🔍 {url}"` | `False` | siempre |
  | B | l. 244 (doc extraíble, antes de emit_sources) | `"✅ {url} ({word_count}w)"` | `True` | siempre |
  | C | l. 260 (binario, antes de emit_sources) | `"✅ {url} (binario)"` | `True` | siempre |
  | D | l. 272 (raw, antes de emit_sources) | `"✅ {url} (raw)"` | `True` | siempre |
  | E | l. 277 (antes de `_extract_content`) | `"📄 Extrayendo…"` | `False` | solo si `verbose=True` |
  | F | l. 296 (antes de `_try_alternate_fallback`) | `"🔄 Buscando alternativa…"` | `False` | solo si `verbose=True` |
  | G | l. 362 (antes de `return result` éxito) | `"✅ {url} ({word_count}w)"` | `True` | siempre |
  | H | l. 367 (`except`) | `"❌ {url}"` | `True` | siempre |

  **Importante**: Asegurar que los retornos tempranos (B, C, D, G, H) emiten
  `done=True` **antes** de `_emit_sources` o del return.  Sin `done=True`
  el shimmer se queda animando para siempre.

  **Verificación**:
  - `verbose=False` (default): solo A + B/C/D/G/H → 2 eventos por fetch
  - `verbose=True`: aparecen E y F adicionales
  - Todos los caminos de retorno (incluyendo validación de URL en l. 210-214)
    producen un `done=True` si hay `__event_emitter__` disponible

  **Riesgo**: Bajo.  Cada inserción es 1-2 líneas.  Si se omite un `done=True`
  es detectable visualmente (shimmer perpetuo).

  ---
  ### Cambio 3 — Status events en `batch_fetch_urls()`

  **Archivo**: `smart_fetch_url.py`
  **Líneas afectadas**: l. 417-460 (`fetch_one` + retorno)

  **Problema de diseño**: si cada `smart_fetch_url` emite sus propios eventos
  (Cambio 2), y además `fetch_one` emite los suyos, hay duplicación.

  **Decisión**: `batch_fetch_urls` pasa el emitter a las sub-llamadas pero
  fuerza `verbose=False` internamente.  Es `fetch_one` quien emite el
  progreso por item con el formato `[i/N]`.

  **Opción A** (recomendada):
  - `batch_fetch_urls` emite un status inicial: `"[0/{n}] Iniciando…"` (done=False)
  - `fetch_one` pasa `__event_emitter__` a `smart_fetch_url`, pero añade
    un wrapper que emite `"[{i+1}/{n}] ✅ {url}"` al completar cada item
    (done=False)
  - Al final del gather: `"✅ Batch completado — {n} URLs"` (done=True)

  **Código sketch para `fetch_one`**:
  ```python
  async def fetch_one(index: int, single_url: str) -> str:
      async with semaphore:
          try:
              result = await self.smart_fetch_url(
                  url=single_url,
                  format=format,
                  max_chars=max_chars,
                  browser=browser,
                  os=os,
                  timeout_ms=timeout_ms,
                  show_favicons=False,
                  __event_emitter__=__event_emitter__,  # ← pasar emitter
                  __user__=__user__,
              )
              await self._emit_status(
                  __event_emitter__,
                  f"[{index + 1}/{len(urls)}] ✅ {single_url}",
              )
              return f"## [{index + 1}/{len(urls)}] {single_url}\n\n{result}\n\n---\n"
          except Exception as e:
              await self._emit_status(
                  __event_emitter__,
                  f"[{index + 1}/{len(urls)}] ❌ {single_url}",
                  done=True,
              )
              return f"## [{index + 1}/{len(urls)}] {single_url}\n\nError: {self._format_error(e, single_url)}\n\n---\n"
  ```

  Pero esto reintroduce el problema: si `smart_fetch_url` ya emite "🔍" y "✅",
  se mezclan con los `[i/N]` del batch.  **Opción B**: crear un flag
  `_suppress_status=False` opcional en `smart_fetch_url` que las sub-llamadas
  activen, desactivando sus propios eventos y dejando que `fetch_one`
  maneje todo el progreso.

  Opción elegida (marcar al implementar): **[ ] A / [ ] B**

  **Verificación**:
  - Batch de 5 URLs emite ~7 eventos: 1 inicial + 5 por item + 1 final
  - No hay duplicación de mensajes "🔍" / "✅"
  - shimmer finaliza con `done=True`

  **Riesgo**: Medio — tocar el flujo batch existente.

  ---
  ### Cambio 4 — Cobertura de `done=True` en todos los retornos

  **Archivo**: `smart_fetch_url.py`

  Revisar que **todos** los puntos de salida emitan `done=True` si hay
  `__event_emitter__` disponible.  Incluyendo:

  | Línea | Condición | Estado hoy |
  |-------|-----------|------------|
  | l. 211 | URL vacía | ❌ return string directo |
  | l. 214 | Protocolo inválido | ❌ return string directo |
  | l. 244 | Documento extraíble | ❌ solo `_emit_sources` |
  | l. 260 | Binario no texto | ❌ solo `_emit_sources` |
  | l. 272 | Formato raw | ❌ solo `_emit_sources` |
  | l. 362 | Éxito normal | ❌ solo `_emit_sources` |
  | l. 367 | Excepción general | ❌ return error msg |

  Los retornos de validación (l. 211, 214) no tienen `__event_emitter__`
  a su alcance (están fuera del `try`) — no emiten eventos porque la
  validación ocurre antes de cualquier operación.  Es aceptable.

  Para el resto, el Cambio 2 ya cubre B/C/D/G/H.  Este cambio es
  simplemente una verificación cruzada.

  **Riesgo**: Muy bajo — solo check list.

  ---
  ### Cambio 5 — Zombie threads: wrapper `_run_in_thread()`

  **Archivo**: `smart_fetch_url.py`
  **Líneas afectadas**: 7 callsites de `asyncio.to_thread`

  **Problema**: `asyncio.to_thread()` no expone el `ThreadPoolExecutor`
  subyacente, así que no podemos hacer `cancel_futures=True`.  Si el
  usuario cancela la generación, la task asyncio se cancela pero el
  thread sigue ejecutándose hasta completar.

  **Solución propuesta**: Sustituir `asyncio.to_thread(func)` por un
  wrapper que use un `ThreadPoolExecutor` propio con
  `cancel_futures=True` en shutdown, más un timeout de seguridad.

  **Código**:

  ```python
  import concurrent.futures

  class Tools:
      _thread_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None

      def _get_thread_pool(self):
          if self._thread_pool is None:
              self._thread_pool = concurrent.futures.ThreadPoolExecutor(
                  max_workers=4, thread_name_prefix="smart_fetch"
              )
          return self._thread_pool

      async def _run_in_thread(self, func, timeout=30.0):
          loop = asyncio.get_running_loop()
          pool = self._get_thread_pool()
          fut = loop.run_in_executor(pool, func)
          try:
              return await asyncio.wait_for(fut, timeout=timeout)
          except asyncio.CancelledError:
              fut.cancel()  # marca la future como cancelada
              # El thread sigue ejecutándose, pero el pool se encargará
              # de él.  Con cancel_futures=True en shutdown se cortan.
              raise
          except asyncio.TimeoutError:
              fut.cancel()
              raise

      # Opcional: cleanup al final de la vida del Tools
      # (Open WebUI no garantiza que __del__ se llame, pero no duele)
      def __del__(self):
          if self._thread_pool is not None:
              self._thread_pool.shutdown(wait=False, cancel_futures=True)
  ```

  **Callsites a reemplazar**:

  | # | Línea actual | Código actual | Reemplazar por |
  |---|-------------|---------------|----------------|
  | 1 | l. 674 | `await asyncio.to_thread(_do_extract)` | `await self._run_in_thread(_do_extract)` |
  | 2 | l. 690 | `await asyncio.to_thread(self._strip_html, raw_html)` | `await self._run_in_thread(lambda: self._strip_html(raw_html))` |
  | 3 | l. 866 | `await asyncio.to_thread(Tools._detect_content_type_sync, raw_html)` | `await self._run_in_thread(lambda: Tools._detect_content_type_sync(raw_html))` |
  | 4 | l. 911 | `await asyncio.to_thread(_do_extract)` | `await self._run_in_thread(_do_extract)` |
  | 5 | l. 954 | `await asyncio.to_thread(_find_alternates)` | `await self._run_in_thread(_find_alternates)` |
  | 6 | l. 1179 | `return await asyncio.to_thread(_do_extract)` | `return await self._run_in_thread(_do_extract)` |
  | 7 | l. 1257 | `return await asyncio.to_thread(_do_extract)` | `return await self._run_in_thread(_do_extract)` |

  **Nota**: `asyncio.to_thread` acepta args posicionales (`*args`).
  Nuestro wrapper no.  Para los casos con argumentos (l. 690, 866),
  usamos `lambda` para capturarlos.

  **Riesgo**: Medio.  Aunque `_run_in_thread` es un sustituto directo,
  hay 7 callsites que tocar y el timeout de 30s podría ser demasiado
  corto para PDFs muy grandes (aunque 30s es generoso).  Se puede
  parametrizar por tipo de operación si hace falta.

  **Limitación conocida**: `concurrent.futures.ThreadPoolExecutor` con
  `cancel_futures=True` solo cancela futures **no iniciadas**.  Una vez
  que el thread ya está ejecutándose, `cancel_futures=True` no lo mata.
  Para eso necesitaríamos algo como `threading.Event` de señalización
  o `PEP 554` (no llegó).  Es mejor que nada, pero no es una solución
  completa.

---

## P1 — Code clarity / maintainability

- [ ] **Rename parameter `os` to `os_profile`**
  `os` shadows the built-in module (`import os` is used at the top of
  the file).  Python allows it, but it's confusing for readers and
  breaks IDE refactoring.  The public method signature changes, but
  callers typically pass it as a keyword argument, so this is a
  backward-compatible change in practice.

- [ ] **Deduplicate selectolax parse in feed path**
  When a page is classified as `"feed"`:
  1. `_detect_content_type()` parses HTML with selectolax
  2. `_basic_extract()` parses the same HTML again with selectolax
  In large forums this doubles parse time (~5ms → ~10ms).  Options:
  - Thread a pre-parsed `HTMLParser` tree through the pipeline
  - Cache the tree on `self` (careful with re-entrancy)
  - Accept the overhead (it's small, but inelegant)

---

## P2 — Production hardening

- [ ] **Rate limiting for batch fetches**
  `batch_fetch_urls()` respects concurrency via `asyncio.Semaphore`,
  but there is no global rate limiter.  50 concurrent requests from
  one chat session can trigger rate-limiting or abuse detection on
  the target servers.  Options:
  - Add a `requests_per_second` valve (default: ~10/s)
  - Use `asyncio.Semaphore` with a token-bucket or sliding-window

- [ ] **Document the `os_profile` change in README**
  When `os` → `os_profile`, update the docstring and the README so
  existing users know.

- [ ] **Add a version bump in the docstring header**
  Current: `version: 0.5.0`
  After all fixes: `version: 0.6.0`

---

## P3 — Nice-to-have

- [ ] **Graceful message for unsupported document formats**
  Currently `.xlsx`, `.pptx`, `.odt`, EPUB, RTF, legacy `.doc` show a
  message saying extraction isn't implemented.  Consider pointing the
  user to Open WebUI's knowledge-base upload as a workaround (already
  done for most, but check consistency).

- [x] **Removed the shared httpx client entirely**
  Done as part of the P0 fix.  `_fetch_with_httpx()` now uses
  `async with httpx.AsyncClient()` per request.
