# Meta webhook fixtures

Trimmed copies of real WhatsApp Cloud API webhook bodies (CLAUDE.md §9).
Phone numbers and ids are fake.

| File | Case |
|---|---|
| `text.json` | Arabic text message |
| `interactive_list_reply.json` | Customer picked a time slot from an interactive list |
| `image.json` | Image, answered with "please type instead" |
| `template_quick_reply.json` | Customer tapped "Reschedule" on a reminder template (`type: button`, payload `rem:1:reschedule`) |
| `status_sent.json` | Delivery receipt: our template was sent |
| `status_failed.json` | Delivery receipt: our send failed with error 131047 (outside the 24h window) |
| `duplicate.json` | The same `wamid` twice in one delivery, which must be stored once |
| `bad_signature.json` | `{signature, payload}`: a forged delivery that must be rejected |

Timestamps are fixed in the past (> 24h ago). Tests that need an open
window rewrite them to "now".
