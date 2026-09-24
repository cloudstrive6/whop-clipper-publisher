"""Whop Content Rewards clipping pipeline."""
try:  # use the Windows certificate store (antivirus/proxy HTTPS inspection breaks certifi)
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass
