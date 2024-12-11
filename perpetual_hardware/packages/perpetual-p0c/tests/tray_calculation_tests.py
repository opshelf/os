from perpetual_p0c.configuration import p0_funnel_config
from perpetual_p0c.funnel import FunnelConfiguration, FunnelOffsets
from perpetual_p0c.tray import Tray


def move_to_cell(configuration: FunnelConfiguration, tray: Tray, idx: int) -> None:
    ipos, opos = tray.get_cell_position(idx)
    diff = opos - ipos
    ipos += configuration.offsets.funnel_cell_1_offset_x
    opos =  configuration.opposing_funnel_max_travel - opos
    opos -= configuration.offsets.funnel_cell_1_offset_x
    opos -= configuration.offsets.funnel_min_width
    opos = min([configuration.opposing_funnel_max_travel, opos])
    ipos = min([configuration.input_funnel_max_travel, ipos])
    opos = max([0, opos])
    print(diff, opos, ipos)

if __name__ == '__main__':
    for slot in range(0, 6):
        move_to_cell(p0_funnel_config, Tray(cells=6,cell_width=50, cell_depth=100), slot)



