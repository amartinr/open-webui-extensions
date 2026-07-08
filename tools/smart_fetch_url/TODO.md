1.  [x] **Remove unused or redundant parameters from the function signature**
    The following parameters should be removed:
      - `os_profile` — never used internally by curl_cffi, only appears in
        output metadata
      - `headers` — redundant; curl_cffi already sets browser-specific
        headers automatically via `impersonate`, and the current code
        overrides them with generic values, undermining fingerprinting
      - `show_favicons` — UI cosmetic feature with no functional impact;
        hardcode to `true`

2.  [x] **Change `include_replies` default from `true` to `false`**
    Including comment threads by default consumes unnecessary tokens and
    context space. Users who need replies can explicitly set it to `true`.

3.  [x] **Update the supported browser list and adapt the code accordingly**
    The current `BROWSER_PROFILES` mapping is outdated and contains many
    invalid entries (~2/3 of profiles don't exist in curl_cffi or use
    incorrect naming). Replace it with the actual list of 36 supported
    profiles (including generic aliases like `chrome`, `firefox`, `safari`,
    etc.) and adapt the code to pass them directly to curl_cffi without the
    broken dictionary lookup.  The default was also changed from
    ``"firefox_147"`` to the ``"firefox"`` alias (curl_cffi resolves it to
    the latest Firefox profile), making task 4 unnecessary.

4.  [x] **Unify `smart_fetch_url` and `batch_fetch_urls` into a single tool**
    Merge both into `smart_fetch_url` with a single `urls` parameter that
    always accepts a `list[str]` (even for a single URL). Internally,
    dispatch to single-fetch or batch logic based on `len(urls)`. This
    eliminates ~180 tokens of redundant tool definition, simplifies the
    agent's decision of which tool to call, and ensures consistent parameter
    names and defaults across both modes.

5.  [x] **Refactor `_format_error` to return structured error data**
    Instead of returning a formatted string directly, `_format_error` should
    return a dictionary with `error_type` and `message` keys, e.g.
    `{"error_type": "dns", "message": "DNS resolution failed"}`. This way the
    caller can use it both for JSON output and for text-based metadata
    without duplicating logic.

6.  **Refactor `_format_output` to build responses from a single metadata dict
    and unify error/success output**
    Construct a single dictionary internally with all fields (status, url,
    title, word count, error info, etc.) and serialize it according to the
    requested format. This replaces the current branching between JSON and
    text paths with a single loop over the dict keys.
    As part of this refactor, add `> Status: ok` on success and
    `> Status: error` on failure as the first metadata line in all text-based
    output formats. On error, include `> Error:` with concise messages
    inspired by real-world server and browser errors:
      - Network errors (DNS, connection refused, timeout, TLS) → descriptive
        messages without HTTP status codes, e.g. `DNS resolution failed`,
        `Connection refused`, `Connection timed out`, `SSL connection error`
      - HTTP errors (403, 404) → standard status line, e.g. `403 Forbidden`,
        `404 Not Found`
      - Catch-all → `Internal error`
      - Global timeout → `The operation timed out`