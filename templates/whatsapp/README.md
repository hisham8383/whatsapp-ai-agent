# WhatsApp message templates

**Submit all five on day 1** (CLAUDE.md §4, §12). Approval is out of our
control and can take from minutes to a few days; a rejection is fixed and
resubmitted the same day. Templates are the **only** way to message someone
outside the 24-hour customer-service window, so each feature below is
dead until its template is approved.

| File | Category | Config key | Sent by | Parameters |
|---|---|---|---|---|
| `appointment_reminder.json` | UTILITY | `TEMPLATE_REMINDER` | scheduler, at each `REMINDER_OFFSETS` | 1 business name, 2 day, 3 time. Quick replies: Confirm / Reschedule |
| `booking_confirmation_update.json` | UTILITY | `TEMPLATE_BOOKING_UPDATE` | inbox, when staff move a booking outside the window | 1 business name, 2 day, 3 time |
| `staff_handoff_alert.json` | UTILITY | `TEMPLATE_STAFF_ALERT` | scheduler, to each `HANDOFF_CONTACTS` number | 1 customer first name, 2 handoff reason |
| `conversation_reopen.json` | UTILITY | `TEMPLATE_REOPEN` | inbox "Re-open with template" button | 1 customer first name, 2 business name |
| `lead_followup.json` | MARKETING | `TEMPLATE_FOLLOWUP` | scheduler, once per lead | 1 customer first name, 2 service, 3 business name. Includes opt-out wording |

Each file holds the Arabic and the English version (`language: ar` / `en`)
under the same name. The code sends the customer's language (staff alerts use
`DEFAULT_LANGUAGE`). The JSON mirrors the Graph API `message_templates` create
payload, so each entry can be posted as-is:

```sh
# One call per language entry. WABA id = WHATSAPP_BUSINESS_ACCOUNT_ID.
curl -X POST "https://graph.facebook.com/v23.0/$WABA_ID/message_templates" \
  -H "Authorization: Bearer $WHATSAPP_ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d @entry.json
```

or paste the text into WhatsApp Manager → Message templates.

Rules:

- The names in the `TEMPLATE_*` config keys must match the approved names
  exactly. The app refuses to start without them.
- The parameter order in the table above is what the code sends. If a template
  changes its parameters, change the sender too (`app/whatsapp/templates.py`
  callers), then resubmit.
- Any wording change needs re-approval. Keep the old name live until the new
  one is approved, then switch the config key.
- `lead_followup` is MARKETING: Meta may throttle it, and it needs the opt-out
  line. Never use it for broadcasts to customer lists (out of scope, §1).
