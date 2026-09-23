"""rewind - find what an identity changed in AWS, and what it was before.

A local CLI. It reads CloudTrail event history and makes read-only Describe calls.
It creates no AWS resources, needs no database, and runs nothing in the account.
"""

__version__ = "0.1.0"
