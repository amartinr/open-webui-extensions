# TODO — Content-Type Detection

## 🔴 Blocking

- [ ] **T2 (blog article)**: find a real article URL with `og:type="article"`
      that responds 200. Try `https://tonsky.me/blog/thermocline/`
      or other well-known blogs.

## 🟡 Heuristic improvements

- [ ] **Table-based feed coverage**: Hacker News, Lobsters and similar sites
      classify as `"unknown"` because they use `<tr class="athing">` layout
      instead of modern CSS classes. Consider adding signal S11 to detect
      repeated rows with classes like `athing`, `row`, etc.
      *Note: trafilatura already extracts these pages well, so this is cosmetic.*

- [ ] **Tune weights/thresholds** so 5 `.post` + pagination = `"feed"` is more
      robust. Currently works but barely crosses the threshold.

- [ ] **Signal for `<table>` + specific classes** (old Reddit style).
      Investigate what CSS classes forums and aggregators with tables use.

## 🟢 Tests

- [ ] **Regression test for `format="raw"`**: verify that `smart_fetch_url()`
      with `format="raw"` never reaches `_detect_content_type()`.

- [ ] **No-regression test for `_try_alternate_fallback()`**: confirm the
      alternate fallback does not trigger for feeds (word_count ≥ 30).

- [ ] **Test `_format_output()`** with each format (markdown, html, txt, json, raw)
      to verify the feed early return produces compatible dicts.

- [ ] **Test `batch_fetch_urls()`** with a mix of feeds and articles.

## 📦 CI / Tooling

- [ ] Add `requirements-test.txt` or `test/requirements.txt` with test
      dependencies.

- [ ] Create `run_tests.sh` script that activates the venv and runs all
      three test modules sequentially.
