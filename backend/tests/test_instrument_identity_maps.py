from app.bot.universe.discovery import exact_instrument_maps


def test_same_ticker_different_broker_instrument_never_becomes_alias():
    canonical = {'SBER':'BBG004730N88','VTBR':'BBG004730ZJ9'}
    forward,reverse=exact_instrument_maps(canonical,[('TCS019029540','SBER'),('TCS40A0JP5V6','VTBR')])
    assert forward == reverse == {}


def test_exact_figi_preserved_regardless_of_prefix_and_row_order():
    canonical={'SBER':'BBG004730N88','VKCO':'TCS00A106YF0'}
    rows=[('TCS019029540','SBER'),('BBG004730N88','SBER'),('TCS00A106YF0','VKCO')]
    expected={figi:figi for figi in canonical.values()}
    assert exact_instrument_maps(canonical,rows)==(expected,expected)
    assert exact_instrument_maps(canonical,reversed(rows))==(expected,expected)
