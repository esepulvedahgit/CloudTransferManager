from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher


class PasswordService:
    def __init__(self) -> None:
        self._hasher = PasswordHash((Argon2Hasher(time_cost=3, memory_cost=65536, parallelism=4),))

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password: str, password_hash: str) -> bool:
        try:
            valid, _updated_hash = self._hasher.verify_and_update(password, password_hash)
            return valid
        except (ValueError, TypeError):
            return False
