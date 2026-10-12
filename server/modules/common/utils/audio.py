import array

def pcm_energy(pcm_s16le: bytes) -> float:
    a = array.array("h")
    a.frombytes(pcm_s16le)
    if not a:
        return 0.0
    return (sum(abs(x) for x in a) / len(a)) / 32768.0
