import pytest

from mathx import fibonacci, gcd, is_prime


@pytest.mark.parametrize("n", [2, 3, 5, 7, 11, 97])
def test_primes(n):
    assert is_prime(n)


@pytest.mark.parametrize("n", [-7, 0, 1, 4, 9, 100])
def test_non_primes(n):
    assert not is_prime(n)


def test_gcd_basic():
    assert gcd(12, 18) == 6
    assert gcd(7, 13) == 1


def test_gcd_is_non_negative_and_handles_zero():
    assert gcd(-12, 18) == 6
    assert gcd(12, -18) == 6
    assert gcd(0, 5) == 5
    assert gcd(0, 0) == 0


def test_fibonacci_sequence():
    assert [fibonacci(i) for i in range(8)] == [0, 1, 1, 2, 3, 5, 8, 13]


def test_fibonacci_negative_raises():
    with pytest.raises(ValueError):
        fibonacci(-1)
