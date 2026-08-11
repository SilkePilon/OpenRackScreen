def test_packages_are_importable():
    import ors_render
    import ors_schema

    assert isinstance(ors_schema.__version__, str)
    assert isinstance(ors_render.__version__, str)
