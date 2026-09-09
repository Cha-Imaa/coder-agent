"""Small integer helpers."""


def is_prime(n: int) -> bool:
    """True for primes; 0, 1 and negatives are not prime."""
    if n < 2:
        return False
    return all(n % d for d in range(2, int(n**0.5) + 1))


def gcd(a: int, b: int) -> int:
    """Greatest common divisor, always non-negative; gcd(0, 0) is 0."""
    a, b = abs(a), abs(b)
    while b:
        a, b = b, a % b
    return a


def fibonacci(n: int) -> int:
    """The n-th Fibonacci number with fibonacci(0) == 0 and fibonacci(1) == 1."""
    if n < 0:
        raise ValueError("n must be non-negative")
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
