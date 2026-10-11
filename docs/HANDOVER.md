# Handover guide

For [Business name]. Fill the `[brackets]` at go-live. Everything described
here runs on **your** accounts: Meta, the AI provider, your cloud, and your
database.

## What the assistant does

- Answers customers' questions 24/7 from your knowledge base (your Q&As,
  services and prices), in **Arabic and English**. It replies in the language
  the customer writes in; Arabic replies use natural Gulf/Saudi phrasing
  following your approved style guide, and formal Arabic to customers who
  write formally.
- **Books, reschedules, and cancels appointments** in [your booking system /
  its own booking list, visible in the staff inbox]. It only offers times that
  are actually free. Before any change it sends the customer the exact details
  with **Confirm / Change** buttons, and nothing changes until they confirm.
- **Hands conversations to your team** when a customer asks for a person, is
  upset or complaining, asks for a discount or exception, or asks something
  the knowledge base doesn't cover. It then tells the customer your team will
  reply, with your staff hours, and stays silent until your team hands the
  chat back. **Your staff get a WhatsApp alert** ([numbers]) and, if you
  chose it, an email ([addresses]) within a minute.
- **Sends appointment reminders** [24 hours and 2 hours] before each booking,
  with **Confirm** / **Reschedule** buttons. "Reschedule" continues straight
  into a chat with the assistant.
- **Follows up once** with customers who asked about a service but didn't
  book, [20 hours] later (never at night: [22:00–09:00]), and never more than
  once every [14] days. Customers can reply STOP / إيقاف at any time.
- **Keeps reminders right** when your team moves or deletes an appointment in
  [Google Calendar], and tells the customer when their appointment moved.

## What it does not do

- **No free-form messages outside the 24-hour window.** WhatsApp only allows
  free-form replies within 24 hours of the customer's last message. After
  that, only the five approved message templates can be sent (reminder,
  booking update, staff alert, conversation re-open, follow-up).
- **No broadcasts.** The follow-up goes only to a customer who asked about a
  specific service, once. Marketing messages to customer lists are not part
  of this system.
- Voice notes, images, and stickers are not read. The assistant politely asks
  the customer to type instead.
- It won't invent prices, discounts, services, or free times, and it ignores
  customers who try to talk it into doing so.

## Staff inbox guide

- **Address:** [https://your-domain/inbox]. **Password:** stored in your cloud's
  secret store as `INBOX_AUTH_SECRET` ([Google Cloud → Security → Secret Manager
  / AWS → Secrets Manager]). Never share it over WhatsApp or email. Changing
  it there (and restarting the service) logs everyone out.
- The list shows conversations waiting for your team, newest first, with a
  short summary written by the assistant. Below it: upcoming bookings.
- Open a conversation to read it and reply. Your reply goes out on WhatsApp
  from your business number.
- **The 24-hour rule:** after 24 hours without a message from the customer, the
  reply box locks. Click **Re-open with template**: the customer gets a short
  approved message asking them to reply, and you can write freely as soon as
  they do. It can be sent once until the customer answers.
- Each upcoming booking shows its reminders (due / sent / failed), and each
  conversation shows the customer's follow-up and opt-out status.
- When you're done, click **Return to bot**. If nobody does, the conversation
  returns to the assistant automatically after [24] hours.



## Editing the knowledge base

Three files, all in the `knowledge/` folder of your repository:

| File | What's in it |
|---|---|
| `business.yaml` | Name, address, map link, opening hours (also the bookable hours), services (names in both languages, duration, price, which staff), staff, booking rules (gap between appointments, minimum notice, how far ahead, cancellation policy) |
| `faqs.yaml` | Questions and answers, each in Arabic and English (`q_ar`, `a_ar`, `q_en`, `a_en`). No limit on how many. |
| `style_ar.md` | How the assistant should sound in Arabic: greetings, confirmations, words to prefer and avoid, your own phrases |

After editing, redeploy (see RUNBOOK). The assistant checks both files when it
starts and **refuses to start** if something is wrong (a missing translation,
unknown staff, a placeholder left in), with a message saying
exactly what to fix, so a typo can't turn into wrong answers. During the
support period, send changes to [Your name] instead if you prefer.

## Running costs

| Item | What for | Typical amount |
|---|---|---|
| WhatsApp (Meta) | Replies, plus template messages (reminders, alerts, re-open, follow-ups) | Replies inside the 24-hour window are free or very low. Each template message is charged by Meta by category (UTILITY for reminders/alerts, MARKETING for follow-ups): see Meta's current Saudi rate card. |
| AI (Anthropic) | Understanding and answering each message, checking the calendar | About $0.0023 per customer message (measured on the 44-conversation test set); 1,000 messages ≈ $2.30. |
| Hosting ([Google Cloud / AWS]) | The API + staff inbox, the worker, and the scheduler, online 24/7 | [amount] per month |
| Database | Customers, bookings, recent conversations | [amount] per month |

Set a monthly spend limit in the Anthropic console and a budget alert in your
cloud account.

## Limits (by design)

- Two languages: Arabic (Gulf/Saudi tuned) and English.
- The assistant remembers about the last 20 messages of a conversation.
- Only one booking system is connected: [name].

## Your data (Saudi PDPL)

The system stores only the customer's WhatsApp number, name, bookings,
conversation text, and opt-out choices. Message text and handoff summaries are
deleted automatically after [90] days (`LOG_RETENTION_DAYS`), and sent
reminders, follow-ups and alerts after 90 days; bookings and opt-outs are kept
(an opt-out must be honored for good). Staff alerts never contain the
customer's phone number. Phone numbers are masked in logs. The assistant never asks
for health details, ID numbers, or card data.

## Removing my access

Remove [your email] from: Meta Business Settings → Users → People; the
Anthropic organization (Settings → Members); your cloud project (IAM);
[your booking system].
