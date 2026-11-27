# SPDX-License-Identifier: Apache-2.0
"""Feature flags in Log Detective (Flagsmith).

Flag changes arrive from Flagsmith webhooks and sit on the timeline next to deploys, restarts and commits. When an
error group starts, the flag changes just before it become suspects, ranked by how close in time they were and by
whether the failing code is the code that checks the flag. A suspect flag can be turned off through the Flagsmith
API after a person approves it, and Tracebase then watches the error: if it stops, the error is marked resolved
by the rollback.

This package is licensed under the Apache License 2.0 (see LICENSE in this folder), unlike the rest of Tracebase.
"""
