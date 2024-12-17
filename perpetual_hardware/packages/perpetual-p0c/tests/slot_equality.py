from perpetual_p0c.tool_board import SlotStatus


if __name__ == '__main__':
    s1 = SlotStatus.from_buffer(0)
    s2 = SlotStatus.from_buffer(0)
    assert(s1 == s2)
    s3 = SlotStatus.from_buffer(1)
    assert(s1 != s3)
