# akita_email/exceptions.py

class AkitaEmailError(Exception):
    """Base exception class for all Akita eMail specific errors."""
    pass

class DatabaseError(AkitaEmailError):
    """Raised for errors related to database operations (SQLite)."""
    pass

class ProtocolError(AkitaEmailError):
    """Raised for errors during message encoding, decoding, or validation."""
    pass

class RoutingError(AkitaEmailError):
    """Raised for errors related to finding routes or next hops (though routing is basic)."""
    pass

class CommunicationError(AkitaEmailError):
    """Raised for errors in communication (Serial, Meshtastic send/receive)."""
    pass

class ConfigurationError(AkitaEmailError):
    """Raised for errors related to invalid configuration settings."""
    pass

