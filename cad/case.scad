// 4-fader controller case: top panel + base tray.
//
// Render a part:  openscad -D 'part="panel"' -o panel.stl case.scad
//                 openscad -D 'part="base"'  -o base.stl  case.scad
// part="assembly" shows both with ghost faders/displays/switches.
//
// Axes: X = width (left->right), Y = depth (0 = front edge, nearest user),
// Z = up. Faders travel along Y.

part = "assembly"; // [assembly, panel, base, clips, exploded]

$fn = 48;
eps = 0.01;

// ---------------------------------------------------------------- shell
wall      = 2.5;
floor_t   = 2.5;
panel_t   = 2.0;   // fader screws: M3 x 4 countersunk
h_in      = 65;    // floor top -> panel underside (room for clip leads)
corner_r  = 4;
fit       = 0.3;   // panel lip -> wall clearance
lip_t     = 1.6;
lip_h     = 3;

// ---------------------------------------------------------------- fader
// Alps RSA0N11M9 (100 mm travel), see datasheet drawing.
fd_len_front = 74;     // frame end -> travel centre, terminal/pot end
fd_len_back  = 72.5;   // travel centre -> frame end, motor end
fd_w         = 17;     // frame width
fd_body_d    = 21;     // frame top -> lowest point of body
fd_body_w    = 19;     // envelope incl. motor, for clearance only
fd_hole_dy   = 60;     // 2x M3 at +-60 (120 +-0.3)
fd_travel    = 100;
fd_lever_w   = 8;      // stem width along travel
fd_lever_t   = 1.5;    // stem thickness across travel
fd_lever_h   = 8.2;    // knob plate top above frame top
slot_w       = 3;
slot_len     = fd_travel + fd_lever_w + 2;
m3_hole      = 3.3;
m3_csk_d     = 6.4;

// ---------------------------------------------------------------- display
// Adafruit 326 0.96in OLED, STEMMA QT revision, from Adafruit's Eagle board
// (github.com/adafruit/Adafruit-128x64-Monochrome-OLED-PCB). Board coords:
// origin at board corner, y=0 is the flex edge (front), 8-pin header along
// the top edge at y=29.21 (back). Glass faces up against panel underside.
od_w      = 29.21;
od_d      = 31.75;
od_pcb_t  = 1.6;
od_holes  = [[2.54, 2.54], [26.67, 2.54], [2.54, 29.21], [26.67, 29.21]];  // 24.13 x 26.67 c-c
od_glass  = [[1.255, 7.673], [27.955, 26.933]];  // 26.7 x 19.26, min, max
od_glass_t = 1.6;       // 1.45 panel + tape; post height
od_active = [[3.733, 13.969], [25.477, 24.833]];  // 21.74 x 10.86
win_margin = 1.0;       // window = active + 1 mm per side
win_chamfer = 1.0;      // top-side bevel; limited by M2.5 pan heads at y=29.21
od_post_d = 4.4;        // clears glass edge at y=26.93
od_screw_d = 2.8;       // M2.5 x 8 pan head from the top, nut under the board

// ---------------------------------------------------------------- buttons
// "tact": Omron B3F-4050 12x12 tactile (as on the PCB): body 12x12x3.5,
//         collar 7.1 dia x 0.8, square 3.8 plunger, 7.3 overall. Pushed up
//         from below into an open-bottom box, collar + plunger through the
//         hole, body top against the panel underside. A retaining bar
//         (part="clips") slides in along x under the body, between the leg
//         pairs (legs at +-x, y = +-2.5), and takes the press. The slot fits
//         bodies tact_body_h..tact_body_h_max: pick the thickest bar that goes in.
// "mx": Cherry MX style keyswitch, 14 x 14 plate cutout, clips need 1.5 plate.
// "round": panel-mount momentary pushbutton, round hole.
btn_type     = "tact";  // [tact, mx, round]
tact_body    = 12.0;
tact_body_h  = 3.5;
tact_collar_h = 0.8;
tact_h       = 7.3;
tact_hole_d  = 7.6;     // clears the 7.1 collar; plunger stands h - body - panel proud
tact_fit     = 0.2;     // per side, body -> box side walls
tact_body_h_max = 4.5;  // tallest body the slot accepts (with the thinnest bar)
tact_wall    = 1.6;
tact_leg_clr = 1.2;     // body edge -> end wall, room for the legs
tact_bar_w   = 3.0;     // fits between the 5.0 pitch leg pairs
tact_bar_t   = 1.6;     // thinnest bar; thickest = + (body_h_max - body_h)
tact_bar_step = 0.2;
clip_t       = 2.5;     // part="clips": 6 bars of this thickness (fits the B3F here); 0 = test set
tact_bar_fit = 0.15;    // per side in the slot
tact_floor   = 1.2;     // end wall below the slot
mx_cut       = 14.0;
mx_plate_t   = 1.5;
mx_pocket    = 15.6;
round_hole_d = 12.2;    // 12 mm pushbutton; 16.2 for 16 mm

// ---------------------------------------------------------------- layout
pitch     = 38;    // fader column pitch
x0        = 10;    // left panel edge -> first column edge
x_right   = 6;     // strip -> right panel edge
fd_y0     = 14;    // panel front edge -> fader frame front end
btn_gap   = 12.5;  // fader frame back end -> fader button centre
od_gap    = 13;    // fader button centre -> display board front edge
y_back    = 9;     // display board back edge -> panel back edge
gen_dy    = 26;    // spacing of the two general buttons

// Back wall carries PCB (155) + PD board (20.32) between the corner bosses.
pd_gap = 6;       // PCB -> PD board, and PD board -> right corner boss
W_back = 2 * (2.5 + 7.5) + 0.5 + 155 + pd_gap + 20.32 + pd_gap;
W = max(x0 + 4 * pitch + 28 + x_right, W_back);
strip_w = W - x0 - 4 * pitch - x_right;   // general-button strip right of the columns
fd_cy  = fd_y0 + fd_len_front;                 // travel centre
fd_y1  = fd_cy + fd_len_back;                  // frame back end
btn_y  = fd_y1 + btn_gap;
od_y0  = btn_y + od_gap;
D = od_y0 + od_d + y_back;

top_z = floor_t + h_in;     // panel underside
H = top_z + panel_t;

function col_x(i) = x0 + pitch / 2 + i * pitch;
gen_x = x0 + 4 * pitch + strip_w / 2;
gen_btns = [[gen_x, btn_y], [gen_x, btn_y + gen_dy]];   // G2 (by fader row), G1
fader_btns = [for (i = [0:3]) [col_x(i), btn_y]];

// ---------------------------------------------------------------- joint
boss_d   = 7.5;
insert_d = 4.0;   // M3 heat-set insert, 5.7 long
insert_h = 6;
pcb_standoff_d = 8;   // PCB: same M3 insert as the bosses
pd_insert_d = 3.2;    // PD board: M2 heat-set insert (M2 screw through its M2.5 holes), <=5.7 long
pd_standoff_d = 6.5;
boss_in  = wall + boss_d / 2;
bosses = [
    [boss_in, boss_in], [W - boss_in, boss_in],
    [boss_in, D - boss_in], [W - boss_in, D - boss_in],
    [x0 + 2 * pitch, boss_in],
];
// Back-middle boss hangs off the back wall above the PCB (PCB + Pico on
// headers stay below hung_z). Sits in the gap between displays 2 and 3.
hung_bosses = [[x0 + 2 * pitch, D - boss_in]];
hung_z = 34;      // above floor top
all_bosses = concat(bosses, hung_bosses);

// ---------------------------------------------------------------- panel ribs
rib_t = 2;
rib_h = 7;

// ---------------------------------------------------------------- PCB
// motorized-faders/motorized-faders.kicad_pcb: Edge.Cuts 55,43 -> 210,136,
// 4x M3 at 5 mm inset. KiCad top edge (y=43, Pico USB end) faces the back
// wall; Pico USB-C centre is 48.89 from the left edge, face ~4.3 mm inside.
pcb_size  = [155, 93];
pcb_back_gap = 0.5;    // board edge -> back wall inner face
pcb_pos   = [wall + boss_d + 0.5, D - wall - pcb_back_gap - 93];  // front-left corner
pcb_holes = [[5, 5], [150, 5], [5, 88], [150, 88]];
pico_usb_x = 48.89;
pcb_standoff_h = 5;
// Pico USB-C centre above floor top: directly soldered vs on female headers
// (8.5 female + 2.5 male header plastic). The opening spans both.
pico_usb_z_low  = pcb_standoff_h + 1.6 + 1 + 1.6;
pico_usb_z_high = pico_usb_z_low + 8.5 + 2.5;

// ---------------------------------------------------------------- PD board
// Adafruit 5807 HUSB238, from Adafruit_CAD_Parts STEP (local only). Board coords:
// y=23.5 edge carries the USB-C (overhangs to y=24.61), 2x M2.5 holes beside
// it. Sits flat on standoffs, USB-C facing the back wall.
pd_size   = [20.32, 23.5];
pd_usb_x  = 10.16;
pd_usb_overhang = 1.11;
pd_usb_zc = 2.67;          // connector centre above board underside
pd_holes  = [[2.54, 20.95], [17.78, 20.95]];
pd_standoff_h = 8;
pd_pos = [pcb_pos.x + pcb_size.x + pd_gap, D - wall - 0.3 - pd_usb_overhang - pd_size.y];

// ---------------------------------------------------------------- back wall IO
// USB-C openings, sized for bulky plug overmolds. [x, z low, z high] centres
usb_cut  = [15, 9];
usb_cuts = [[pcb_pos.x + pico_usb_x, pico_usb_z_low, pico_usb_z_high],
            [pd_pos.x + pd_usb_x, pd_standoff_h + pd_usb_zc, pd_standoff_h + pd_usb_zc]];

feet = [[15, 15], [W - 15, 15], [15, D - 15], [W - 15, D - 15]];
foot_d = 10.5;
foot_recess = 1;

// ================================================================= helpers
module rrect(size, r, h) {
    linear_extrude(h)
        offset(r) offset(-r) square(size);
}

module csk_m3(depth) {
    // countersunk M3 through-hole, top face at z=0, going down
    translate([0, 0, -depth - eps]) cylinder(d = m3_hole, h = depth + 2 * eps);
    translate([0, 0, -(m3_csk_d - m3_hole) / 2])
        cylinder(d1 = m3_hole, d2 = m3_csk_d, h = (m3_csk_d - m3_hole) / 2 + eps);
}

// ================================================================= panel
module display_cut() {
    a0 = od_active[0] - [win_margin, win_margin];
    a1 = od_active[1] + [win_margin, win_margin];
    s = a1 - a0;
    hull() {
        translate([a0.x, a0.y, -eps]) cube([s.x, s.y, eps]);
        translate([a0.x - win_chamfer, a0.y - win_chamfer, panel_t])
            cube([s.x + 2 * win_chamfer, s.y + 2 * win_chamfer, eps]);
    }
    translate([a0.x, a0.y, -1]) cube([s.x, s.y, panel_t + 2]);
}

module display_posts() {
    for (h = od_holes) translate([h.x, h.y, -od_glass_t])
        cylinder(d = od_post_d, h = od_glass_t + eps);
}

tact_bot_z = -tact_body_h;                   // switch underside (nominal), rel. panel underside
tact_slot_top = tact_bot_z + tact_bar_fit;
tact_slot_bot = -tact_body_h_max - tact_bar_t - tact_bar_fit;
tact_bar_ts = [for (t = [tact_bar_t : tact_bar_step : tact_bar_t + tact_body_h_max - tact_body_h + eps]) t];

tact_c = tact_body / 2 + tact_fit;               // box inner half-width (y)
tact_e = tact_body / 2 + tact_leg_clr;           // box inner half-length (x)
tact_bar_len = 2 * (tact_e + tact_wall) + 3;     // pokes 1.5 out each end

module button_cradle() {
    if (btn_type == "tact") {
        zb = tact_slot_bot - tact_floor;
        difference() {
            union() {
                // side walls, locate the body in y
                for (sy = [-1, 1]) translate([-tact_e - tact_wall, sy > 0 ? tact_c : -tact_c - tact_wall, -tact_body_h_max])
                    cube([2 * (tact_e + tact_wall), tact_wall, tact_body_h_max + eps]);
                // end walls, outside the legs, carry the bar
                for (sx = [-1, 1]) translate([sx > 0 ? tact_e : -tact_e - tact_wall, -tact_c - tact_wall, zb])
                    cube([tact_wall, 2 * (tact_c + tact_wall), -zb + eps]);
            }
            // bar slot along x, spans every body height in range
            translate([-tact_e - tact_wall - 1, -tact_bar_w / 2 - tact_bar_fit, tact_slot_bot])
                cube([2 * (tact_e + tact_wall) + 2, tact_bar_w + 2 * tact_bar_fit, tact_slot_top - tact_slot_bot]);
        }
    }
}

module button_bar(t = tact_bar_t, dots = 0) {
    // flat, with a head at one end as stop / grip; dots on the head mark the size
    translate([-tact_bar_len / 2, -tact_bar_w / 2, 0]) cube([tact_bar_len, tact_bar_w, t]);
    translate([tact_bar_len / 2 - 1.5, -tact_bar_w / 2 - 1.5, 0]) {
        cube([1.5, tact_bar_w + 3, t]);
        if (dots > 0) for (i = [0:dots - 1]) translate([1.5, 0.6 + i * 1.0, 0]) cube([0.6, 0.6, t]);
    }
}

module button_cut() {
    if (btn_type == "tact") {
        translate([0, 0, -1]) cylinder(d = tact_hole_d, h = panel_t + 2);
    } else if (btn_type == "mx") {
        translate([-mx_cut / 2, -mx_cut / 2, -1]) cube([mx_cut, mx_cut, panel_t + 2]);
        translate([-mx_pocket / 2, -mx_pocket / 2, -1])
            cube([mx_pocket, mx_pocket, 1 + panel_t - mx_plate_t]);
    } else {
        translate([0, 0, -1]) cylinder(d = round_hole_d, h = panel_t + 2);
    }
}

module fader_cut() {
    // slot, with top bevel
    hull() {
        translate([-slot_w / 2, -slot_len / 2, -1]) cube([slot_w, slot_len, panel_t + 1 - 0.8]);
        translate([-slot_w / 2 - 0.8, -slot_len / 2 - 0.8, panel_t]) cube([slot_w + 1.6, slot_len + 1.6, eps]);
    }
    for (s = [-1, 1]) translate([0, s * fd_hole_dy, panel_t]) csk_m3(panel_t);
}

module panel() {
    difference() {
        union() {
            translate([0, 0, top_z]) rrect([W, D], corner_r, panel_t);
            // alignment lip inside the walls
            translate([0, 0, top_z - lip_h]) difference() {
                translate([wall + fit, wall + fit, 0])
                    rrect([W - 2 * (wall + fit), D - 2 * (wall + fit)], corner_r - wall, lip_h + eps);
                translate([wall + fit + lip_t, wall + fit + lip_t, -eps])
                    rrect([W - 2 * (wall + fit + lip_t), D - 2 * (wall + fit + lip_t)], 1, lip_h + 3 * eps);
                for (b = all_bosses) translate([b.x, b.y, -eps]) cylinder(d = boss_d + 1, h = lip_h + 3 * eps);
            }
            // stiffening ribs between fader bodies
            for (i = [0:4]) translate([x0 + i * pitch - rib_t / 2, fd_y0, top_z - rib_h])
                cube([rib_t, fd_y1 - fd_y0, rib_h + eps]);
            for (i = [0:3]) translate([col_x(i) - od_w / 2, od_y0, top_z]) display_posts();
            for (b = concat(fader_btns, gen_btns)) translate([b.x, b.y, top_z]) button_cradle();
        }
        for (i = [0:3]) translate([col_x(i), fd_cy, top_z]) fader_cut();
        for (i = [0:3]) translate([col_x(i) - od_w / 2, od_y0, top_z]) display_cut();
        for (b = concat(fader_btns, gen_btns)) translate([b.x, b.y, top_z]) button_cut();
        for (b = all_bosses) translate([b.x, b.y, H]) csk_m3(panel_t + 1);
        for (i = [0:3], h = od_holes) translate([col_x(i) - od_w / 2 + h.x, od_y0 + h.y, top_z - od_glass_t - 1])
            cylinder(d = od_screw_d, h = od_glass_t + panel_t + 2);
    }
}

// ================================================================= base
module base() {
    difference() {
        union() {
            difference() {
                rrect([W, D], corner_r, top_z);
                translate([wall, wall, floor_t])
                    rrect([W - 2 * wall, D - 2 * wall], corner_r - wall, h_in + 1);
            }
            // bosses, joined to the front/back wall
            for (b = bosses) hull() {
                translate([b.x, b.y, 0]) cylinder(d = boss_d, h = top_z);
                translate([b.x - boss_d / 2, b.y < D / 2 ? 0 : D - eps, 0]) cube([boss_d, eps, top_z]);
            }
            // hung bosses with a 45 deg support underneath
            for (b = hung_bosses) hull() {
                translate([b.x, b.y, floor_t + hung_z]) cylinder(d = boss_d, h = top_z - floor_t - hung_z);
                translate([b.x - boss_d / 2, D - eps, floor_t + hung_z - (D - b.y + boss_d / 2)])
                    cube([boss_d, eps, top_z - floor_t - hung_z + D - b.y + boss_d / 2]);
            }
            for (h = pcb_holes) translate([pcb_pos.x + h.x, pcb_pos.y + h.y, 0])
                cylinder(d = pcb_standoff_d, h = floor_t + pcb_standoff_h);
            for (h = pd_holes) translate([pd_pos.x + h.x, pd_pos.y + h.y, 0])
                cylinder(d = pd_standoff_d, h = floor_t + pd_standoff_h);
        }
        for (b = all_bosses) translate([b.x, b.y, top_z - insert_h]) cylinder(d = insert_d, h = insert_h + eps);
        for (h = pcb_holes) translate([pcb_pos.x + h.x, pcb_pos.y + h.y, floor_t + pcb_standoff_h - insert_h])
            cylinder(d = insert_d, h = insert_h + eps);
        for (h = pd_holes) translate([pd_pos.x + h.x, pd_pos.y + h.y, floor_t + pd_standoff_h - insert_h])
            cylinder(d = pd_insert_d, h = insert_h + eps);
        for (u = usb_cuts) translate([u.x, D + eps, floor_t]) rotate([90, 0, 0])
            linear_extrude(wall + 2 * eps + 1) hull()
                for (z = [u[1], u[2]]) translate([0, z])
                    offset(2) offset(-2) square(usb_cut, center = true);
        for (f = feet) translate([f.x, f.y, -eps]) cylinder(d = foot_d, h = foot_recess + eps);
    }
}

// ================================================================= ghosts
module ghost_fader() {
    color("silver") {
        translate([-fd_w / 2, -fd_len_front, -1]) cube([fd_w, fd_len_front + fd_len_back, 1]);
        translate([-fd_body_w / 2, -fd_len_front, -fd_body_d]) cube([fd_body_w, fd_len_front + fd_len_back, fd_body_d - 1]);
    }
    color("dimgray") {
        translate([-fd_lever_t / 2, -fd_lever_w / 2, 0]) cube([fd_lever_t, fd_lever_w, fd_lever_h - 4]);
        translate([-fd_lever_t / 2, -18.5 / 2, fd_lever_h - 4]) cube([fd_lever_t, 18.5, 4]);
    }
}

module ghost_display() {
    color("midnightblue") translate([0, 0, -od_glass_t - od_pcb_t]) cube([od_w, od_d, od_pcb_t]);
    color("black") translate([od_glass[0].x, od_glass[0].y, -od_glass_t])
        cube([od_glass[1].x - od_glass[0].x, od_glass[1].y - od_glass[0].y, od_glass_t]);
}

module ghost_button() {
    if (btn_type == "tact") color("tan") {
        translate([-6, -6, tact_bot_z]) cube([12, 12, tact_body_h]);
        translate([0, 0, tact_bot_z + tact_body_h]) cylinder(d = 7.1, h = tact_collar_h);
        translate([-1.9, -1.9, tact_bot_z]) cube([3.8, 3.8, tact_h]);
        translate([0, 0, tact_slot_bot]) button_bar(tact_bot_z - tact_slot_bot);
    } else if (btn_type == "mx") color("tan") {
        translate([-7, -7, -5]) cube([14, 14, 5]);
        translate([-9, -9, 6.6]) cube([18, 18, 8]);
    } else color("tan") translate([0, 0, -10]) cylinder(d = 12, h = 14);
}

module ghosts() {
    for (i = [0:3]) translate([col_x(i), fd_cy, top_z]) ghost_fader();
    for (i = [0:3]) translate([col_x(i) - od_w / 2, od_y0, top_z]) ghost_display();
    for (b = concat(fader_btns, gen_btns)) translate([b.x, b.y, top_z]) ghost_button();
    color("green", 0.5) translate([pcb_pos.x, pcb_pos.y, floor_t + pcb_standoff_h]) cube([pcb_size.x, pcb_size.y, 1.6]);
    color("black") translate([pd_pos.x, pd_pos.y, floor_t + pd_standoff_h]) {
        cube([pd_size.x, pd_size.y, 1.57]);
        translate([pd_usb_x - 4.47, 16.71, 0.57]) cube([8.94, 7.9, 4.2]);
    }
}

// ================================================================= output
echo(str("case outer: ", W, " x ", D, " x ", H, " mm"));
echo(str("fader underside clearance to floor: ", top_z - fd_body_d - floor_t, " mm"));

if (part == "panel") {
    // print face down
    translate([0, D, H]) rotate([180, 0, 0]) panel();
} else if (part == "clips") {
    // button retaining bars: a test set (n dots = n-th size from thinnest), or 6 of clip_t
    if (clip_t == 0) for (i = [0:len(tact_bar_ts) - 1]) {
        echo(str("bar ", i + 1, " dot(s): ", tact_bar_ts[i], " mm -> body ", tact_body_h_max - (tact_bar_ts[i] - tact_bar_t), " mm"));
        translate([0, i * (tact_bar_w + 6), 0]) button_bar(tact_bar_ts[i], i + 1);
    } else for (i = [0:len(concat(fader_btns, gen_btns)) - 1])
        translate([0, i * (tact_bar_w + 6), 0]) button_bar(clip_t);
} else if (part == "base") {
    base();
} else if (part == "exploded") {
    color("lightsteelblue") base();
    translate([0, 0, 60]) { color("whitesmoke") panel(); ghosts(); }
} else if (part == "assembly") {
    color("lightsteelblue") base();
    color("whitesmoke") panel();
    ghosts();
}
