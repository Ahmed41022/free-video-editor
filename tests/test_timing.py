import unittest

from video_converter.timing import (
    FADE_MAX,
    MIN_PART,
    SNAP_EDGE,
    SPEED_MAX,
    SPEED_MIN,
    atempo_factors,
    audio_filters,
    clamp_speed,
    cut_range,
    fade_filters,
    kept_segments,
    normalize_keep,
    output_duration,
    part_index,
    relative,
    remove_part_at,
    removed_parts,
    restore_part_at,
    select_filters,
    source_to_output,
    split_at,
    split_points,
)


class NormalizeKeepTests(unittest.TestCase):
    def test_sorts_and_merges_overlapping_ranges(self):
        self.assertEqual(normalize_keep([(5, 7), (0, 2), (1, 3)], 10), [(0, 3), (5, 7)])
        self.assertEqual(normalize_keep([(1, 6), (2, 4)], 10), [(1, 6)])

    def test_touching_ranges_stay_as_a_split(self):
        self.assertEqual(normalize_keep([(0, 3), (3, 10)], 10), [(0, 3), (3, 10)])
        self.assertEqual(normalize_keep([(4, 6), (2, 4)], 10), [(2, 4), (4, 6)])

    def test_a_hairline_gap_counts_as_touching(self):
        self.assertEqual(normalize_keep([(0, 3), (3.0005, 10)], 10), [(0, 3), (3.0, 10)])

    def test_ranges_near_the_ends_snap_to_them(self):
        self.assertEqual(normalize_keep([(0.04, 5)], 10), [(0.0, 5)])
        self.assertEqual(normalize_keep([(2, 9.96)], 10), [(2, 10.0)])
        # Just outside the snap distance, the range stays as it is.
        self.assertEqual(normalize_keep([(SNAP_EDGE + 0.01, 5)], 10), [(SNAP_EDGE + 0.01, 5)])
        self.assertEqual(normalize_keep([(2, 10 - SNAP_EDGE - 0.01)], 10), [(2, 10 - SNAP_EDGE - 0.01)])

    def test_ranges_are_clamped_to_the_duration(self):
        self.assertEqual(normalize_keep([(2, 15)], 10), [(2, 10)])
        self.assertEqual(normalize_keep([(-1, 4)], 10), [(0, 4)])
        self.assertEqual(normalize_keep([(12, 20)], 10), [])

    def test_without_a_duration_nothing_is_clamped_or_collapsed(self):
        self.assertEqual(normalize_keep([(2, 15)]), [(2, 15)])
        self.assertEqual(normalize_keep([(0, 10)]), [(0, 10)])

    def test_tiny_ranges_are_dropped(self):
        self.assertEqual(normalize_keep([(1, 1.005), (3, 4)], 10), [(3, 4)])
        self.assertEqual(normalize_keep([(1, 1.005)], 10), [])

    def test_the_whole_video_means_no_cuts(self):
        self.assertEqual(normalize_keep([(0, 10)], 10), [])
        self.assertEqual(normalize_keep([(0.03, 9.98)], 10), [])
        self.assertEqual(normalize_keep([(0, 4), (3, 10)], 10), [])     # overlapping ranges that cover it all
        self.assertEqual(normalize_keep([], 10), [])

    def test_two_touching_parts_covering_everything_are_still_a_split(self):
        self.assertEqual(normalize_keep([(0, 5), (5, 10)], 10), [(0, 5), (5, 10)])

    def test_normalizing_twice_changes_nothing(self):
        for ranges in ([(5, 7), (0.04, 2), (1, 3)], [(0, 3), (3.0004, 9.99)], [(2, 4), (4, 6), (8, 12)]):
            once = normalize_keep(ranges, 10)
            self.assertEqual(normalize_keep(once, 10), once)


class PartQueryTests(unittest.TestCase):
    KEEP = [(0.0, 3.0), (3.0, 6.0), (8.0, 10.0)]

    def test_removed_parts_include_leading_and_trailing_gaps(self):
        self.assertEqual(removed_parts([(2.0, 5.0), (7.0, 9.0)], 10.0), [(0.0, 2.0), (5.0, 7.0), (9.0, 10.0)])
        self.assertEqual(removed_parts([(0.0, 4.0), (6.0, 10.0)], 10.0), [(4.0, 6.0)])
        self.assertEqual(removed_parts([(2.0, 5.0)], 10.0), [(0.0, 2.0), (5.0, 10.0)])

    def test_nothing_is_removed_without_cuts_or_between_splits(self):
        self.assertEqual(removed_parts([], 10.0), [])
        self.assertEqual(removed_parts([(0.0, 3.0), (3.0, 10.0)], 10.0), [])

    def test_removed_parts_with_an_unknown_duration_skip_the_trailing_gap(self):
        self.assertEqual(removed_parts([(2.0, 5.0)], None), [(0.0, 2.0)])

    def test_split_points_are_where_ranges_touch(self):
        self.assertEqual(split_points(self.KEEP), [3.0])
        self.assertEqual(split_points([(0.0, 3.0), (3.0, 6.0), (6.0, 10.0)]), [3.0, 6.0])
        self.assertEqual(split_points([(0.0, 3.0), (4.0, 6.0)]), [])
        self.assertEqual(split_points([]), [])

    def test_part_index_finds_the_kept_range_under_a_time(self):
        self.assertEqual(part_index(self.KEEP, 1.0, 10.0), 0)
        self.assertEqual(part_index(self.KEEP, 3.0, 10.0), 1)       # a split belongs to the part after it
        self.assertEqual(part_index(self.KEEP, 5.99, 10.0), 1)
        self.assertEqual(part_index(self.KEEP, 9.0, 10.0), 2)
        self.assertEqual(part_index(self.KEEP, 10.0, 10.0), 2)      # the very end belongs to the last part

    def test_part_index_is_none_inside_a_cut(self):
        self.assertIsNone(part_index(self.KEEP, 6.0, 10.0))
        self.assertIsNone(part_index(self.KEEP, 7.0, 10.0))
        self.assertIsNone(part_index(self.KEEP, 12.0, 10.0))

    def test_part_index_without_cuts_is_the_whole_video(self):
        self.assertEqual(part_index([], 5.0, 10.0), 0)
        self.assertEqual(part_index([], 10.0, 10.0), 0)
        self.assertIsNone(part_index([], 11.0, 10.0))


class SplitAndRemoveTests(unittest.TestCase):
    def test_splitting_without_cuts_makes_two_touching_ranges(self):
        self.assertEqual(split_at([], 4.0, 10.0), [(0.0, 4.0), (4.0, 10.0)])

    def test_splitting_inside_a_part_keeps_the_other_parts(self):
        self.assertEqual(split_at([(0.0, 3.0), (6.0, 10.0)], 8.0, 10.0), [(0.0, 3.0), (6.0, 8.0), (8.0, 10.0)])

    def test_splitting_too_close_to_an_edge_changes_nothing(self):
        self.assertEqual(split_at([], 0.05, 10.0), [])
        self.assertEqual(split_at([], 9.95, 10.0), [])
        self.assertEqual(split_at([(2.0, 6.0)], 2.0 + MIN_PART - 0.01, 10.0), [(2.0, 6.0)])
        self.assertEqual(split_at([(2.0, 6.0)], 6.0 - MIN_PART + 0.01, 10.0), [(2.0, 6.0)])

    def test_splitting_just_far_enough_from_an_edge_works(self):
        self.assertEqual(split_at([], 0.2, 10.0), [(0.0, 0.2), (0.2, 10.0)])

    def test_splitting_inside_a_cut_changes_nothing(self):
        keep = [(0.0, 3.0), (6.0, 10.0)]
        self.assertEqual(split_at(keep, 4.0, 10.0), keep)

    def test_splitting_twice_at_the_same_point_changes_nothing(self):
        once = split_at([], 4.0, 10.0)
        self.assertEqual(split_at(once, 4.0, 10.0), once)

    def test_remove_part_needs_more_than_one_part(self):
        self.assertEqual(remove_part_at([], 5.0, 10.0), [])
        self.assertEqual(remove_part_at([(2.0, 6.0)], 3.0, 10.0), [(2.0, 6.0)])

    def test_remove_part_cuts_out_the_part_under_the_time(self):
        keep = [(0.0, 4.0), (4.0, 10.0)]
        self.assertEqual(remove_part_at(keep, 1.0, 10.0), [(4.0, 10.0)])
        self.assertEqual(remove_part_at(keep, 7.0, 10.0), [(0.0, 4.0)])
        three = [(0.0, 3.0), (3.0, 6.0), (6.0, 10.0)]
        self.assertEqual(remove_part_at(three, 4.0, 10.0), [(0.0, 3.0), (6.0, 10.0)])

    def test_removing_the_last_part_leaves_a_range_that_is_not_the_whole_video(self):
        self.assertEqual(remove_part_at([(0.0, 3.0), (3.0, 10.0)], 8.0, 10.0), [(0.0, 3.0)])

    def test_remove_part_inside_a_cut_changes_nothing(self):
        keep = [(0.0, 3.0), (6.0, 10.0)]
        self.assertEqual(remove_part_at(keep, 4.0, 10.0), keep)

    def test_restore_puts_back_the_cut_part_as_a_split(self):
        self.assertEqual(restore_part_at([(0.0, 3.0), (6.0, 10.0)], 4.0, 10.0),
                         [(0.0, 3.0), (3.0, 6.0), (6.0, 10.0)])

    def test_restore_leading_and_trailing_cuts(self):
        self.assertEqual(restore_part_at([(2.0, 10.0)], 1.0, 10.0), [(0.0, 2.0), (2.0, 10.0)])
        self.assertEqual(restore_part_at([(0.0, 6.0)], 8.0, 10.0), [(0.0, 6.0), (6.0, 10.0)])

    def test_restore_on_a_kept_part_changes_nothing(self):
        keep = [(0.0, 3.0), (6.0, 10.0)]
        self.assertEqual(restore_part_at(keep, 1.0, 10.0), keep)
        self.assertEqual(restore_part_at([], 1.0, 10.0), [])

    def test_remove_then_restore_gives_back_the_same_picture(self):
        keep = split_at(split_at([], 3.0, 10.0), 6.0, 10.0)
        removed = remove_part_at(keep, 4.0, 10.0)
        self.assertEqual(removed, [(0.0, 3.0), (6.0, 10.0)])
        self.assertEqual(restore_part_at(removed, 4.0, 10.0), keep)

    def test_inputs_are_not_modified(self):
        keep = [(0.0, 3.0), (3.0, 10.0)]
        snapshot = list(keep)
        split_at(keep, 1.0, 10.0)
        remove_part_at(keep, 1.0, 10.0)
        restore_part_at(keep, 1.0, 10.0)
        cut_range(keep, 1.0, 2.0, 10.0)
        self.assertEqual(keep, snapshot)


class CutRangeTests(unittest.TestCase):
    def test_cutting_the_middle(self):
        self.assertEqual(cut_range([], 3.0, 5.0, 10.0), [(0.0, 3.0), (5.0, 10.0)])

    def test_cutting_the_start_or_the_end(self):
        self.assertEqual(cut_range([], 0.0, 3.0, 10.0), [(3.0, 10.0)])
        self.assertEqual(cut_range([], 7.0, 10.0, 10.0), [(0.0, 7.0)])

    def test_cut_overlapping_the_edges_of_two_parts(self):
        self.assertEqual(cut_range([(0.0, 3.0), (5.0, 10.0)], 2.0, 6.0, 10.0), [(0.0, 2.0), (6.0, 10.0)])

    def test_several_cuts_add_up(self):
        keep = cut_range([], 1.0, 2.0, 10.0)
        keep = cut_range(keep, 4.0, 5.0, 10.0)
        self.assertEqual(keep, [(0.0, 1.0), (2.0, 4.0), (5.0, 10.0)])

    def test_a_cut_swallowing_a_whole_part_removes_it(self):
        keep = [(0.0, 2.0), (3.0, 4.0), (6.0, 10.0)]
        self.assertEqual(cut_range(keep, 2.5, 5.0, 10.0), [(0.0, 2.0), (6.0, 10.0)])

    def test_a_cut_through_a_split_keeps_the_other_split(self):
        self.assertEqual(cut_range([(0.0, 4.0), (4.0, 10.0)], 6.0, 7.0, 10.0),
                         [(0.0, 4.0), (4.0, 6.0), (7.0, 10.0)])

    def test_cutting_a_gap_that_is_already_cut_changes_nothing(self):
        keep = [(0.0, 3.0), (6.0, 10.0)]
        self.assertEqual(cut_range(keep, 3.5, 5.5, 10.0), keep)

    def test_refuses_to_cut_everything(self):
        self.assertEqual(cut_range([], 0.0, 10.0, 10.0), [])
        self.assertEqual(cut_range([], -5.0, 20.0, 10.0), [])
        self.assertEqual(cut_range([(2.0, 5.0)], 0.0, 10.0, 10.0), [(2.0, 5.0)])


class KeptSegmentsTests(unittest.TestCase):
    def test_no_cuts_and_no_trim_is_the_whole_video(self):
        self.assertEqual(kept_segments([], None, None, 10.0), [(0.0, 10.0)])

    def test_trim_applies_on_top_of_the_kept_ranges(self):
        self.assertEqual(kept_segments([(1.0, 3.0), (5.0, 9.0)], 2.0, 8.0, 10.0), [(2.0, 3.0), (5.0, 8.0)])

    def test_trim_alone(self):
        self.assertEqual(kept_segments([], 2.0, 7.0, 10.0), [(2.0, 7.0)])
        self.assertEqual(kept_segments([], 2.0, None, 10.0), [(2.0, 10.0)])

    def test_splits_are_joined_back_together(self):
        keep = [(0.0, 3.0), (3.0, 6.0), (8.0, 10.0)]
        self.assertEqual(kept_segments(keep, None, None, 10.0), [(0.0, 6.0), (8.0, 10.0)])

    def test_a_trim_through_a_split_does_not_leave_a_seam(self):
        self.assertEqual(kept_segments([(0.0, 5.0), (5.0, 10.0)], 2.0, 8.0, 10.0), [(2.0, 8.0)])

    def test_unknown_duration_without_cuts_has_an_open_end(self):
        self.assertEqual(kept_segments([], None, None, None), [(0.0, None)])
        self.assertEqual(kept_segments([], 2.0, None, None), [(2.0, None)])
        self.assertEqual(kept_segments([], None, 5.0, None), [(0.0, 5.0)])

    def test_unknown_duration_with_cuts_uses_the_kept_ranges(self):
        self.assertEqual(kept_segments([(1.0, 3.0)], None, None, None), [(1.0, 3.0)])

    def test_everything_trimmed_away_is_empty(self):
        self.assertEqual(kept_segments([(1.0, 3.0)], 4.0, 8.0, 10.0), [])
        self.assertEqual(kept_segments([(5.0, 9.0)], None, 4.0, 10.0), [])
        self.assertEqual(kept_segments([], 5.0, 5.0, 10.0), [])

    def test_a_part_touched_by_the_trim_only_at_its_edge_is_dropped(self):
        self.assertEqual(kept_segments([(1.0, 3.0), (5.0, 9.0)], 3.0, None, 10.0), [(5.0, 9.0)])


class SpeedMathTests(unittest.TestCase):
    SEGMENTS = [(1.0, 3.0), (5.0, 9.0)]            # 6 s of the source

    def test_output_duration(self):
        self.assertEqual(output_duration(self.SEGMENTS), 6.0)
        self.assertEqual(output_duration(self.SEGMENTS, 2.0), 3.0)
        self.assertEqual(output_duration(self.SEGMENTS, 0.5), 12.0)
        self.assertEqual(output_duration([(0.0, 4.0)], 4.0), 1.0)

    def test_output_duration_is_unknown_for_an_open_end(self):
        self.assertIsNone(output_duration([(0.0, None)]))
        self.assertIsNone(output_duration([(0.0, 2.0), (3.0, None)], 2.0))

    def test_speed_is_clamped_to_the_supported_range(self):
        self.assertEqual(output_duration([(0.0, 8.0)], 100.0), 2.0)       # treated as 4x
        self.assertEqual(output_duration([(0.0, 1.0)], 0.01), 4.0)        # treated as 0.25x
        self.assertEqual((clamp_speed(0.1), clamp_speed(9.0), clamp_speed(1.5)), (SPEED_MIN, SPEED_MAX, 1.5))

    def test_source_to_output_skips_the_cut_parts(self):
        self.assertEqual(source_to_output(1.0, self.SEGMENTS), 0.0)
        self.assertEqual(source_to_output(2.0, self.SEGMENTS), 1.0)
        self.assertEqual(source_to_output(5.0, self.SEGMENTS), 2.0)
        self.assertEqual(source_to_output(6.0, self.SEGMENTS), 3.0)
        self.assertEqual(source_to_output(9.0, self.SEGMENTS), 6.0)       # the very end of the last part

    def test_source_to_output_is_none_inside_a_cut(self):
        for t in (0.5, 3.0, 4.0, 9.5):
            with self.subTest(t=t):
                self.assertIsNone(source_to_output(t, self.SEGMENTS))

    def test_source_to_output_with_speed(self):
        self.assertEqual(source_to_output(6.0, self.SEGMENTS, speed=2.0), 1.5)
        self.assertEqual(source_to_output(6.0, self.SEGMENTS, speed=0.5), 6.0)

    def test_source_to_output_reversed(self):
        self.assertEqual(source_to_output(2.0, self.SEGMENTS, reverse=True), 5.0)
        self.assertEqual(source_to_output(1.0, self.SEGMENTS, reverse=True), 6.0)
        self.assertEqual(source_to_output(9.0, self.SEGMENTS, reverse=True), 0.0)
        self.assertEqual(source_to_output(2.0, self.SEGMENTS, speed=2.0, reverse=True), 2.5)
        self.assertIsNone(source_to_output(4.0, self.SEGMENTS, reverse=True))

    def test_source_to_output_with_an_open_end(self):
        self.assertEqual(source_to_output(5.0, [(0.0, None)]), 5.0)
        self.assertEqual(source_to_output(5.0, [(2.0, None)], speed=2.0), 1.5)
        self.assertIsNone(source_to_output(1.0, [(2.0, None)]))
        self.assertIsNone(source_to_output(5.0, [(0.0, None)], reverse=True))     # the length isn't known


class AtempoTests(unittest.TestCase):
    def test_every_supported_speed_splits_into_valid_steps(self):
        for i in range(int(round((SPEED_MAX - SPEED_MIN) / 0.05)) + 1):
            speed = round(SPEED_MIN + 0.05 * i, 2)
            with self.subTest(speed=speed):
                factors = atempo_factors(speed)
                self.assertTrue(all(0.5 <= f <= 2.0 for f in factors), factors)
                product = 1.0
                for f in factors:
                    product *= f
                self.assertAlmostEqual(product, speed, delta=1e-9)

    def test_known_chains(self):
        self.assertEqual(atempo_factors(1.0), [])
        self.assertEqual(atempo_factors(2.0), [2.0])
        self.assertEqual(atempo_factors(0.5), [0.5])
        self.assertEqual(atempo_factors(4.0), [2.0, 2.0])
        self.assertEqual(atempo_factors(0.25), [0.5, 0.5])
        self.assertEqual(atempo_factors(3.0), [2.0, 1.5])
        self.assertEqual(atempo_factors(0.3), [0.5, 0.6])

    def test_out_of_range_speeds_are_clamped(self):
        self.assertEqual(atempo_factors(10.0), [2.0, 2.0])
        self.assertEqual(atempo_factors(0.0), [0.5, 0.5])


class FilterStringTests(unittest.TestCase):
    def test_a_single_segment_only_resets_the_timestamps(self):
        self.assertEqual(select_filters("[0:v]", "[v]", [(0.0, 3.0)], audio=False), ["[0:v]setpts=PTS-STARTPTS[v]"])
        self.assertEqual(select_filters("[0:a]", "[a]", [(0.0, 3.0)], audio=True), ["[0:a]asetpts=PTS-STARTPTS[a]"])

    def test_three_video_segments_are_split_trimmed_and_concatenated(self):
        filters = select_filters("[0:v]", "[v]", [(0.0, 1.0), (2.0, 3.0), (5.0, 6.5)], audio=False)
        self.assertEqual(filters, [
            "[0:v]split=3[vin_0][vin_1][vin_2]",
            "[vin_0]trim=start=0:end=1,setpts=PTS-STARTPTS[vseg_0]",
            "[vin_1]trim=start=2:end=3,setpts=PTS-STARTPTS[vseg_1]",
            "[vin_2]trim=start=5:end=6.5,setpts=PTS-STARTPTS[vseg_2]",
            "[vseg_0][vseg_1][vseg_2]concat=n=3:v=1:a=0[v]",
        ])

    def test_labels_carry_the_tag_so_clips_do_not_collide(self):
        a = select_filters("[0:v]", "[x]", [(0.0, 1.0), (2.0, 3.0)], audio=False, tag="0")
        b = select_filters("[1:v]", "[y]", [(0.0, 1.0), (2.0, 3.0)], audio=False, tag="1")
        self.assertIn("[vin0_0]", "".join(a))
        self.assertIn("[vin1_0]", "".join(b))
        self.assertFalse(set("".join(a).split("[")) & {"vin1_0]", "vseg1_0]"})

    def test_an_open_ended_segment_has_no_end(self):
        filters = select_filters("[0:v]", "[v]", [(0.0, 1.0), (2.0, None)], audio=False)
        self.assertIn("[vin_1]trim=start=2,setpts=PTS-STARTPTS[vseg_1]", filters)

    def test_relative_segments_start_from_the_first(self):
        self.assertEqual(relative([(1.0, 2.0), (3.0, 4.0), (6.0, None)]), [(0.0, 1.0), (2.0, 3.0), (5.0, None)])

    def test_audio_with_nothing_to_do_is_left_alone(self):
        self.assertEqual(audio_filters("[0:a:0]", "[a]", [(2.0, 5.0)]), "")
        self.assertEqual(audio_filters("[0:a:0]", "[a]", [(0.0, 5.0)], speed=1.0, reverse=False), "")

    def test_audio_can_be_forced_through_a_graph(self):
        self.assertEqual(audio_filters("[0:a:0]", "[a]", [(2.0, 5.0)], always=True),
                         "[0:a:0]asetpts=PTS-STARTPTS[a]")

    def test_audio_with_three_segments_is_cut_relative_to_the_first(self):
        graph = audio_filters("[0:a:0]", "[a]", [(1.0, 2.0), (3.0, 4.0), (6.0, 8.0)])
        self.assertEqual(graph, ";".join([
            "[0:a:0]asplit=3[ain_0][ain_1][ain_2]",
            "[ain_0]atrim=start=0:end=1,asetpts=PTS-STARTPTS[aseg_0]",
            "[ain_1]atrim=start=2:end=3,asetpts=PTS-STARTPTS[aseg_1]",
            "[ain_2]atrim=start=5:end=7,asetpts=PTS-STARTPTS[aseg_2]",
            "[aseg_0][aseg_1][aseg_2]concat=n=3:v=0:a=1[a]",
        ]))

    def test_audio_speed_uses_an_atempo_chain(self):
        self.assertEqual(audio_filters("[0:a:0]", "[a]", [(0.0, 6.0)], speed=2.0),
                         "[0:a:0]asetpts=PTS-STARTPTS[asel];[asel]atempo=2[a]")
        self.assertTrue(audio_filters("[0:a:0]", "[a]", [(0.0, 6.0)], speed=3.0).endswith(
            "[asel]atempo=2,atempo=1.5[a]"))
        self.assertTrue(audio_filters("[0:a:0]", "[a]", [(0.0, 6.0)], speed=0.25).endswith(
            "[asel]atempo=0.5,atempo=0.5[a]"))

    def test_audio_chain_order_is_speed_then_reverse_then_fades(self):
        graph = audio_filters("[0:a:0]", "[a]", [(0.0, 8.0)], speed=2.0, reverse=True, fade_in=0.5, fade_out=1.0)
        self.assertTrue(graph.endswith("[asel]atempo=2,areverse,afade=t=in:st=0:d=0.5,afade=t=out:st=3:d=1[a]"), graph)

    def test_fade_out_starts_relative_to_the_length_after_speed(self):
        self.assertEqual(fade_filters(0.0, 1.0, 4.0, audio=False), ["fade=t=out:st=3:d=1"])
        # 8 s of source at 2x is 4 s of export, so a 1 s fade out starts at 3 s.
        graph = audio_filters("[0:a:0]", "[a]", [(0.0, 8.0)], speed=2.0, fade_out=1.0)
        self.assertIn("afade=t=out:st=3:d=1", graph)
        # Cuts count too: 2 s + 3 s kept is 5 s, or 2.5 s at 2x.
        graph = audio_filters("[0:a:0]", "[a]", [(0.0, 2.0), (4.0, 7.0)], speed=2.0, fade_out=0.5)
        self.assertIn("afade=t=out:st=2:d=0.5", graph)

    def test_fade_in_and_out_names(self):
        self.assertEqual(fade_filters(0.5, 1.0, 4.0, audio=False), ["fade=t=in:st=0:d=0.5", "fade=t=out:st=3:d=1"])
        self.assertEqual(fade_filters(0.5, 1.0, 4.0, audio=True), ["afade=t=in:st=0:d=0.5", "afade=t=out:st=3:d=1"])
        self.assertEqual(fade_filters(0.0, 0.0, 4.0, audio=False), [])

    def test_fades_longer_than_the_clip_are_shortened(self):
        self.assertEqual(fade_filters(10.0, 0.0, 4.0, audio=False), ["fade=t=in:st=0:d=4"])
        self.assertEqual(fade_filters(0.0, 10.0, 4.0, audio=False), ["fade=t=out:st=0:d=4"])

    def test_an_unknown_length_allows_a_fade_in_but_not_a_fade_out(self):
        self.assertEqual(fade_filters(1.0, 2.0, None, audio=False), ["fade=t=in:st=0:d=1"])

    def test_limits_are_the_documented_ones(self):
        self.assertEqual((SPEED_MIN, SPEED_MAX, FADE_MAX), (0.25, 4.0, 30.0))


if __name__ == "__main__":
    unittest.main()
