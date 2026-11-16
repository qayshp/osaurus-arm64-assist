# Provider label and upgrade migration

The default provider label is **NPU Hybrid**. It is short enough to identify the companion in Osaurus's compact provider badge. The label is a display name: CPU/GPU baselines and stock-engine delegation remain available, and the actual execution metadata determines which route ran.

Fresh connections use the concise name. When upgrading an already connected installation, the installer reconnects its existing owned provider. An unchanged legacy default, **Osaurus ARM64 Assist**, is renamed in place. Existing custom labels are preserved unless you explicitly pass a new `-ProviderName`.

```powershell
# After updating the installed scripts, reconnect/migrate the owned provider.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Connect-Osaurus.ps1 -RootPath $AssistRoot

# Optional deliberate custom label; subsequent ordinary upgrades preserve it.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Connect-Osaurus.ps1 -RootPath $AssistRoot -ProviderName 'My local AI'
```

The migration uses Osaurus's typed **PUT `/admin/providers/{existing-id}`** update with a JSON object containing only `name`. On the installed Osaurus 1.0.3 implementation, the update clones the existing provider and retains its ID, API key/reference, URL, enabled/default-model settings and headers when those fields are omitted. It does not delete/recreate the provider or rewrite its credential. Model IDs and agent/default-agent references therefore keep the same identity. The compact label is not a model rename.

Before updating, the connector verifies the installation marker, unique recorded provider, API ID, type, endpoint, enabled state and presence of its credential. Another provider already using the requested name, a changed endpoint/identity, or ambiguous ownership causes a refusal. It preserves those records for inspection rather than adopting an unrelated provider.

Registration writes are atomic, and connection/removal operations share a per-installation lock. The connector records pending rename intent before the HTTP request. If a response or final state write is interrupted, reconnecting can recognize either the previous or intended label by the same provider ID and complete the operation. Disconnecting resolves that same pending identity before its usual scoped removal; it does not create a replacement provider. If unexpected provider settings changed concurrently, the pending record is retained for review instead of blindly overwriting those changes.

The registration file under the private installation's `state` directory contains ownership/reference metadata. Keep it private and do not edit it to claim a different provider. `Status.ps1 -Json` checks the running companion; the Osaurus provider/model picker identifies available entries. A successful label update alone does not load a model or demonstrate NPU execution.

The source contract was verified by metadata/IL inspection of the installed Windows app. Isolated PowerShell 5.1/7 tests cover the migration, collisions, edited/foreign records, interrupted HTTP updates and custom-label preservation. A local HTTP fixture checks UTF-8 name-only PUT, proxy bypass and redirect refusal. These checks use synthetic providers and never alter the user's application configuration.
