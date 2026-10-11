"""Webhook intake.

``verify`` checks authenticity, ``parse`` types the payload, ``dedupe`` drops
repeat deliveries, ``service`` stores and enqueues, and ``routes`` wires it to HTTP.
"""
