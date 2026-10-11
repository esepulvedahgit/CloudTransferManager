from app.security.passwords import PasswordService


def test_password_service_hashes_with_argon2id_and_verifies():
    service = PasswordService()
    encoded = service.hash("CorrectHorseBatteryStaple123!")

    assert encoded.startswith("$argon2id$")
    assert service.verify("CorrectHorseBatteryStaple123!", encoded)
    assert not service.verify("wrong password", encoded)
