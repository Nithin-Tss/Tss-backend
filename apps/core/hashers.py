from django.contrib.auth.hashers import Argon2PasswordHasher


class LightArgon2PasswordHasher(Argon2PasswordHasher):
    """Argon2id with the OWASP-recommended 19 MiB memory cost.

    Django's default uses ~100 MiB per hash; several concurrent sign-ins on a
    small host (gunicorn runs many threads) could exhaust RAM and get the
    process killed, which shows up as a 502. Existing hashes keep verifying
    (their parameters are stored in the hash) and are upgraded on next login.
    """

    time_cost = 2
    memory_cost = 19456
    parallelism = 2
