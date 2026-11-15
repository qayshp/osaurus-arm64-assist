# Idle unload follows Osaurus

Companion 0.1.1 automatically follows the global **Idle unload** preference in **Osaurus → Settings → Models → Local engine**. Set the interval there, including **Never unload**. Normal setup needs no separate idle configuration in the companion.

The default `idle_source` is `"osaurus"`. Existing configurations that omit that field also follow Osaurus after upgrading. The packaged companion 0.1.0 acceptance records remain a record of that earlier package; this behavior is introduced in 0.1.1.

## How following works

A separate watcher reads the current Osaurus preference about every five seconds. A successful read updates the effective interval without restarting the companion. Network requests run outside the inference lock and memory guard, so waiting for a settings response does not block their supervision.

The companion keeps its own activity clock because it runs as an external provider. Inference activity refreshes that clock; health checks, model discovery and preference polls leave it unchanged. Idle unloading waits until inference has finished. A shorter preference can therefore release an already idle worker after the next refresh, while **Never unload** disables automatic idle expiry.

The companion still uses one model worker at a time. Switching profiles releases the previous worker. Explicit unload/stop and the memory-pressure guard can also release a worker. The next request loads the selected profile again.

## Offline and restart behavior

The last valid idle value is cached locally for the same Osaurus endpoint. This includes zero, which represents **Never unload**. The cached value remains usable if Osaurus becomes unreachable and across companion restarts. When a later read succeeds, the live preference replaces it.

If there is no usable live or cached value, the companion uses `idle_seconds`, which defaults to **300 seconds**. That field is a fallback while following Osaurus; it does not replace a valid Osaurus preference.

The cache is `state/idle-policy.json` inside the installation. It contains the numeric idle setting plus schema, loopback-endpoint and update-time metadata. It does not retain the full Osaurus settings response or credentials. A cache from another endpoint is not used for the currently selected endpoint.

## See the effective policy

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Status.ps1 -Json
```

This command uses the default installation directory; add `-RootPath` for a custom installation. The same `idle_policy` object is available in the companion's `GET /health` response. A typical successful read looks like:

```json
{
  "idle_policy": {
    "mode": "osaurus",
    "seconds": 300,
    "status": "live",
    "error": null,
    "setting": "idleUnloadSeconds"
  }
}
```

| Field | Meaning |
|---|---|
| `mode` | `osaurus` follows the app; `fixed` uses the advanced override |
| `seconds` | Effective idle interval; `0` disables automatic idle expiry |
| `status` | `live`, `cached`, `fallback` or `fixed` |
| `error` | A diagnostic code, or `null`; no raw response, key or local path |
| `setting` | The Osaurus setting read by the adapter: `idleUnloadSeconds` |

`cached` and `fallback` distinguish a previously observed preference from the companion's fallback. For example, `mode: "osaurus"`, `seconds: 0`, `status: "cached"` means the last valid **Never unload** preference remains effective while Osaurus is unavailable.

An error can coexist with `live`: `cache_write_failed` means the current preference was read and applied, but its on-disk cache could not be updated. Check the installation directory's permissions if that persists.

## Discovery and authentication

With the default `osaurus_url: "auto"`, the reader takes the port from `%LOCALAPPDATA%\osaurus\server.json` and connects to `127.0.0.1`. If that metadata cannot provide a valid port, it tries port 1337. It accepts only loopback HTTP origins, bypasses proxies and refuses redirects.

For an alternate Osaurus profile, the advanced `osaurus_url` field can specify its loopback origin, for example `http://127.0.0.1:1347`. The endpoint scopes the last-known-value cache. Automatic discovery uses the standard Osaurus data folder; an explicit origin is useful for a profile running elsewhere.

If Osaurus already requires a local admin access key, the reader can reuse `OSAURUS_API_KEY` from the companion process's environment. This is the Osaurus local admin key; the companion provider has its own installation credential. Authentication failures are reported as `http_401` or `http_403` and use cached/fallback policy. The companion does not alter Osaurus authentication settings.

## Advanced fixed override

For a deliberately independent timer, these fields in the existing companion configuration select a fixed interval:

```json
{
  "idle_source": "fixed",
  "idle_seconds": 300
}
```

`idle_seconds` accepts an integer from 0 through 86400. Zero disables automatic idle expiry. To resume automatic following, set `idle_source` to `"osaurus"`; `idle_seconds` then supplies only the unavailable-setting fallback. The usual defaults are:

```json
{
  "idle_source": "osaurus",
  "idle_seconds": 300,
  "osaurus_url": "auto"
}
```

Merge these fields into the existing configuration when using an advanced override. Most users should keep the default and choose their interval in Osaurus.

## Compatibility boundary

The adapter reads `idleUnloadSeconds` using `GET /admin/runtime-settings`. This is an **internal Windows app API observed on Osaurus 1.0.3**, rather than a published stable provider interface. The reader validates a nonnegative integer, bounds the response and timeout, and reports invalid/unavailable responses through its status. A future app change can therefore produce cached/fallback behavior.

The [official Windows model documentation](https://docs.osaurus.ai/windows/models#the-engine) describes the global Idle unload preference. It does not document this internal read route. External-provider requests have their own activity lifecycle; the companion applies the same selected interval to its worker. This adapter does not change Osaurus settings.
