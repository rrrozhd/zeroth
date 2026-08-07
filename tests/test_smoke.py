def test_import():
    """The distribution is importable and its canonical packages carry docs."""
    import zeroth.contracts
    import zeroth.runtime
    import zeroth.service

    assert zeroth.runtime.__doc__
    assert zeroth.contracts.__doc__
    assert zeroth.service.__doc__
