import re
from functools import reduce
from itertools import repeat
from logging import warn
import numpy as np
import pandas as pd
from tqdm import tqdm
from unidecode import unidecode

from nlstruct.core.pandas import make_merged_names_map, merge_with_spans, make_id_from_merged, flatten


def make_tag_scheme(length, entity, scheme='bio'):
    if entity is None:
        return [None] * length
    if scheme == "bio":
        return [f"B-{entity}", *(f"I-{entity}" for _ in range(length - 1))]
    elif scheme == "bioul":
        return [f"B-{entity}", *(f"I-{entity}" for _ in range(length - 2)), f"L-{entity}"] if length >= 2 else [f"U-{entity}"]
    raise ValueError(f"'{scheme}' scheme is not supported")


class DeltaCollection(object):
    def __init__(self, begins, ends, deltas):
        self.begins = np.asarray(begins, dtype=int)
        self.ends = np.asarray(ends, dtype=int)
        self.deltas = np.asarray(deltas, dtype=int)

    @classmethod
    def from_absolute(cls, begins, ends, deltas):
        deltas = np.asarray(deltas)
        shift = np.roll(deltas, 1)
        shift[0] = 0
        deltas -= shift
        return DeltaCollection(begins, ends, deltas)

    def __repr__(self):
        return "DeltaCollection([{}], [{}], [{}])".format(", ".join(map(str, self.begins)),
                                                          ", ".join(map(str, self.ends)),
                                                          ", ".join(map(str, self.deltas)))

    def apply(self, positions, side='left'):
        positions = np.asarray(positions)
        to_add = ((positions.reshape(-1, 1) >= self.ends.reshape(1, -1)) * self.deltas).sum(axis=1)
        between = np.logical_and(self.begins.reshape(1, -1) < positions.reshape(-1, 1),
                                 positions.reshape(-1, 1) < self.ends.reshape(1, -1))
        between_mask = between.any(axis=1)
        between = between[between_mask]
        between_i = between.argmax(axis=1)
        if side == 'right':
            to_add[between_mask] += self.ends[between_i] - positions[between_mask] + self.deltas[between_i]
        elif side == 'left':
            to_add[between_mask] += self.begins[between_i] - positions[between_mask]
        return positions + to_add

    def unapply(self, positions, side='left'):
        positions = np.asarray(positions)
        begins = self.apply(self.begins, side='left')
        ends = self.apply(self.ends, side='right')
        to_remove = -((positions.reshape(-1, 1) >= ends.reshape(1, -1)) * self.deltas).sum(axis=1)
        between = np.logical_and(begins.reshape(1, -1) < positions.reshape(-1, 1),
                                 positions.reshape(-1, 1) < ends.reshape(1, -1))
        between_mask = between.any(axis=1)
        between = between[between_mask]
        between_i = between.argmax(axis=1)
        if side == 'right':
            to_remove[between_mask] += ends[between_i] - positions[between_mask] - self.deltas[between_i]
        elif side == 'left':
            to_remove[between_mask] += begins[between_i] - positions[between_mask]
        pos = positions + to_remove
        return pos

    def __add__(self, other):
        if len(self.begins) == 0:
            return other
        if len(other.begins) == 0:
            return self
        begins = self.unapply(other.begins, side='left')
        ends = self.unapply(other.ends, side='right')
        new_begins = np.concatenate([begins, self.begins])
        new_ends = np.concatenate([ends, self.ends])
        new_deltas = np.concatenate([other.deltas, self.deltas])
        sorter = np.lexsort((new_ends, new_begins))
        return DeltaCollection(new_begins[sorter], new_ends[sorter], new_deltas[sorter])


def make_str_from_groups(replacement, groups):
    for i, group in enumerate(groups):
        replacement = replacement.replace(f"\\{i+1}", group)
    return replacement


def regex_sub_with_spans(pattern, replacement, text):
    needed_groups = [int(i) for i in re.findall(r"\\([0-9]+)", replacement)]
    begins = []
    ends = []
    deltas = []
    for match in reversed(list(re.finditer(pattern, text))):
        middle = make_str_from_groups(replacement, [match.group(i) for i in needed_groups])
        start = match.start()
        end = match.end()
        text = text[:start] + middle + text[end:]
        begins.append(start)
        ends.append(end)
        deltas.append(len(middle) - end + start)
    return text, DeltaCollection(begins, ends, deltas)


def regex_multisub_with_spans(patterns, replacements, text, deltas=None):
    if deltas is None:
        deltas = DeltaCollection([], [], [])
    for pattern, replacement in zip(patterns, replacements):
        text, new_deltas = regex_sub_with_spans(pattern, replacement, text)
        if deltas is not None:
            deltas += new_deltas
        else:
            deltas = new_deltas
    return text, deltas


def run_unidecode(text):
    begins, ends, deltas = [], [], []
    new_text = ""
    for i, (old_char, new_char) in enumerate((char, unidecode(char)) for char in text):
        if len(old_char) != len(new_char):
            begins.append(i)
            ends.append(i+1)
            deltas.append(len(new_char)-1)
        new_text += new_char
    return new_text, DeltaCollection(begins, ends, deltas)


def transform_text(dataset,
                   global_patterns=None,
                   global_replacements=None,
                   apply_unidecode=False,
                   return_deltas=True, with_tqdm=False):
    assert (global_patterns is None) == (global_replacements is None)
    if global_patterns is None:
        global_patterns = []
        global_replacements = []

    def process_text(text, doc_patterns, doc_replacements):
        deltas = None
        if apply_unidecode:
            text, deltas = run_unidecode(text)
        text, deltas = regex_multisub_with_spans(
            [*doc_patterns, *global_patterns],
            [*doc_replacements, *global_replacements],
            text,
            deltas=deltas,
        )
        return text, deltas.begins, deltas.ends, deltas.deltas

    if return_deltas:
        text, delta_begins, delta_ends, deltas = zip(*[
            process_text(text, doc_patterns, doc_replacements) for text, doc_patterns, doc_replacements in
            tqdm(zip(
                dataset["text"],
                dataset["patterns"] if "patterns" in dataset.columns else repeat([]),
                dataset["replacements"] if "replacements" in dataset.columns else repeat([])), total=len(dataset), disable=not with_tqdm)
        ])
        dataset = pd.DataFrame({
            "text": text,
            "begin": delta_begins,
            "end": delta_ends,
            "delta": deltas,
            **{c: dataset[c] for c in dataset.columns if c not in ("text", "begin", "end", "delta")}
        })
        return (
            dataset[[c for c in dataset.columns if c not in ("begin", "end", "delta")]],
            flatten(dataset[["doc_id", "begin", "end", "delta"]]))
    else:
        new_texts = []
        for text, doc_patterns, doc_replacements in tqdm(zip(
              dataset["text"],
              dataset["patterns"] if "patterns" in dataset.columns else repeat([]),
              dataset["replacements"] if "replacements" in dataset.columns else repeat([])), total=len(dataset), disable=not with_tqdm):
            if apply_unidecode:
                text = unidecode(text)
            for pattern, replacement in zip([*doc_patterns, *global_patterns], [*doc_replacements, *global_replacements]):
                text = re.sub(pattern, replacement, text)
            new_texts.append(text)
        dataset = pd.DataFrame({"text": new_texts,
                                **{c: dataset[c] for c in dataset.columns if c not in ("text",)}})
        return dataset[[c for c in dataset.columns if c not in ("begin", "end", "delta")]]


def apply_deltas(positions, deltas, on, position_columns=None):
    if not isinstance(on, (tuple, list)):
        on = [on]
    if position_columns is None:
        position_columns = {'begin': 'left', 'end': 'right'}

    positions = positions.copy()
    positions['_id_col'] = np.arange(len(positions))

    mention_deltas = merge_with_spans(positions[[*position_columns, *on, '_id_col']], deltas, on=on,
                                      suffixes=('_pos', '_delta'), how='inner')
    # To be faster, we remove categorical columns (they may only be in 'on') before the remaining ops
    mention_deltas = mention_deltas[[c for c in mention_deltas.columns if c not in on]]
    positions = positions.set_index('_id_col')
    mention_deltas = mention_deltas.set_index('_id_col')

    delta_col_map, positions_col_map = make_merged_names_map(deltas.columns, [*position_columns, *on, '_id_col'],
                                                             left_on=on, right_on=on, suffixes=('_delta', '_pos'))
    for col, side in position_columns.items():
        mention_deltas.eval(f"shift = ({delta_col_map['end']} <= {positions_col_map[col]}) * {delta_col_map['delta']}",
                            inplace=True)
        mention_deltas.eval(
            f"between_magnet = {delta_col_map['begin']} < {positions_col_map[col]} and {positions_col_map[col]} < {delta_col_map['end']}",
            inplace=True)
        if side == "left":
            mention_deltas.eval(
                f"between_magnet = between_magnet * ({delta_col_map['begin']} - {positions_col_map[col]})",
                inplace=True)
        elif side == "right":
            mention_deltas.eval(
                f"between_magnet = between_magnet * ({delta_col_map['end']} + {delta_col_map['delta']} - {positions_col_map[col]})",
                inplace=True)
        order = "first" if side == "left" else "last"
        tmp = mention_deltas.sort_values(['_id_col', delta_col_map['begin' if side == 'left' else 'end']]).groupby(
            '_id_col').agg({
            "shift": "sum",
            **{n: order for n in mention_deltas.columns if n not in ("shift", "_id_col")}})
        positions[col] = positions[col].add(tmp['shift'] + tmp['between_magnet'], fill_value=0)
    positions = positions.reset_index(drop=True)
    return positions


def reverse_deltas(positions, deltas, on, position_columns=None):
    if not isinstance(on, (tuple, list)):
        on = [on]
    if position_columns is None:
        position_columns = {'begin': 'left', 'end': 'right'}

    positions = positions.copy()
    positions['_id_col'] = np.arange(len(positions))

    deltas = apply_deltas(deltas, deltas, on, position_columns={'begin': 'left', 'end': 'right'})
    mention_deltas = merge_with_spans(positions[[*position_columns, *on, '_id_col']], deltas, on=on,
                                      suffixes=('_pos', '_delta'), how='left')

    positions = positions.set_index('_id_col')
    mention_deltas = mention_deltas.set_index('_id_col')

    # To be faster, we remove categorical columns (they may only be in 'on') before the remaining ops
    # mention_deltas = mention_deltas[[c for c in mention_deltas.columns if c not in on]]
    delta_col_map, positions_col_map = make_merged_names_map(deltas.columns, [*position_columns, *on, '_id_col'],
                                                             left_on=on, right_on=on, suffixes=('_delta', '_pos'))
    for col, side in position_columns.items():
        mention_deltas.eval(
            f"shift = ({delta_col_map['end']} <= {positions_col_map[col]}) * (-{delta_col_map['delta']})",
            inplace=True)
        mention_deltas.eval(
            f"between_magnet = {delta_col_map['begin']} < {positions_col_map[col]} and {positions_col_map[col]} < {delta_col_map['end']}",
            inplace=True)
        if side == "left":
            mention_deltas.eval(
                f"between_magnet = between_magnet * ({delta_col_map['begin']} - {positions_col_map[col]})",
                inplace=True)
        elif side == "right":
            mention_deltas.eval(
                f"between_magnet = between_magnet * ({delta_col_map['end']} - {delta_col_map['delta']} - {positions_col_map[col]})",
                inplace=True)
        order = "first" if side == "left" else "last"

        tmp = mention_deltas.sort_values(['_id_col', delta_col_map['begin' if side == 'left' else 'end']])

        tmp = tmp.groupby('_id_col').agg({
            "shift": "sum",
            **{n: order for n in mention_deltas.columns if n not in ("shift", "_id_col")}})
        positions[col] = positions[col].add(tmp['shift'] + tmp['between_magnet'], fill_value=0).astype(int)
    positions = positions.reset_index(drop=True)
    return positions


def preprocess_ids(large, small, large_id_cols=None, small_id_cols=None):
    # Define on which columns we're going to operate
    if small_id_cols is None:
        small_id_cols = [c for c in small.columns if c.endswith("_id") and c not in ("begin", "end")]
    if large_id_cols is None:
        large_id_cols = [c for c in large.columns if c.endswith("_id") and c not in ("begin", "end")]
    doc_id_cols = [c for c in small.columns if c.endswith("_id") and c in large.columns and c not in ("begin", "end")]
    return (
        doc_id_cols,
        [c for c in small_id_cols if c not in doc_id_cols],
        [c for c in large_id_cols if c not in doc_id_cols],
        [c for c in small.columns if c not in small_id_cols and c not in ("begin", "end") and c not in doc_id_cols],
        [c for c in large.columns if c not in large_id_cols and c not in ("begin", "end") and c not in doc_id_cols])


def encode_as_tag(small, large, label_cols=None, tag_names=None, tag_scheme="bio", use_token_idx=False, verbose=0, groupby=None):
    """

    Parameters
    ----------
    tag_names: str or list of str
        tag name that will be created for each label
    tag_scheme: str
        BIO/BIOUL tagging scheme
    small: tokens
    large: mentions
    label_cols: "label"
    use_token_idx: Use token pos instead of char spans, defaults to False
    verbose: int
        If verbose > 0, make progress bar

    Returns
    -------
    pd.DataFrame
    """
    assert tag_scheme in ("bio", "bioul", "raw")

    doc_id_cols, small_id_cols, large_id_cols, small_val_cols, large_val_cols = preprocess_ids(large, small)
    # assert len(large_val_cols) < 2, "Cannot encode more than one column as tags"
    assert len(large_val_cols) > 0, "Must have a column to encode as tags"
    if label_cols is None:
        label_cols = large_val_cols
    if isinstance(label_cols, str):
        label_cols = [label_cols]
    if tag_names is None:
        tag_names = label_cols
    if isinstance(tag_names, str):
        tag_names = [tag_names]

    label_categories = {}
    # Map mentions to small as a tag
    large = large.sort_values([*doc_id_cols, "begin", "end"])
    for label, mentions_of_group in (large.groupby(groupby, as_index=False, observed=True) if groupby is not None else [(None, large)]):
        assert label not in large_val_cols, f"Cannot groupby {label} value because there is already a column with this name"
        group_tag_names = ["/".join(s for s in (label, tag_name) if s is not None) for tag_name in tag_names]
        if use_token_idx:
            merged = merge_with_spans(mentions_of_group, small[[*doc_id_cols, *small_id_cols, *(c for c in small_val_cols if c != "token_idx"), "token_idx"]], on=doc_id_cols, suffixes=('_large', '')).query(
                "begin <= token_idx and token_idx < end")
        else:
            merged = merge_with_spans(mentions_of_group, small, span_policy='partial_strict', on=[*doc_id_cols, ("begin", "end")], suffixes=('_large', ''))

        # If a token overlap multiple mentions, assign it to the last mention
        len_before = len(merged)
        merged = merged.drop_duplicates([*doc_id_cols, *small_id_cols], keep='last')
        if len_before-len(merged) > 0:
            warn(f"Dropped {len_before-len(merged)} duplicated tags caused by overlapping mentions")
        merged_id_cols = doc_id_cols + large_id_cols + small_id_cols

        # Encode mention labels as a tag
        tags = (merged[merged_id_cols + label_cols]
                .sort_values(merged_id_cols))
        if tag_scheme != "raw":
            keep_cols = list(set(doc_id_cols + large_id_cols) - set(label_cols))
            tags = (
                # convert all categorical dtypes of group cols as simple types (np.str, np.int, np.object...)
                # to accelerate concatenation inside the groupby
                tags.astype({k: dtype if not hasattr(dtype, 'categories') else dtype.categories.dtype for k, dtype in tags.dtypes[keep_cols].items()})
                    .rename(dict(zip(label_cols, group_tag_names)), axis=1)
                    .nlstruct.groupby_assign(
                    doc_id_cols + large_id_cols,
                    {tag_name: lambda labels: make_tag_scheme(len(labels), labels.iloc[0], tag_scheme)
                     for tag_name, label_col in zip(group_tag_names, label_cols)})
                    # convert back each group column dtype to its origial categorical dtype
                    .astype(tags.dtypes[keep_cols])[doc_id_cols + small_id_cols + group_tag_names]
            )

        # merged = merged[[*merged_id_cols, *small_val_cols, "begin", "end"]].merge(tags)
        small = small.merge(tags, on=doc_id_cols + small_id_cols, how="left")
        if tag_scheme != "raw":
            try:
                for tag_name, label_col in zip(group_tag_names, label_cols):
                    unique_labels = sorted(set(label for label in mentions_of_group[label_col] if label is not None))\
                        if not hasattr(mentions_of_group[label_col], 'cat') else mentions_of_group[label_col].cat.categories
                    label_categories[tag_name] = unique_labels
                    small[tag_name] = small[tag_name].fillna("O").astype(pd.CategoricalDtype(
                        ["O", *(tag for label in unique_labels for tag in ("B-" + str(label), "I-" + str(label)))] if tag_scheme == "bio" else
                        ["O", *(tag for label in unique_labels for tag in ("B-" + str(label), "I-" + str(label), "L-" + str(label), "U-" + str(label)))]
                    ))
            except Exception:
                raise Exception(f"Error occured during the encoding of label column '{label_col}' into tag '{tag_name}'")
    # return small[doc_id_cols + small_id_cols].merge(merged, how='left')
    return small, label_categories


def partition_spans(smalls, large,
                    overlap_policy="merge_large",
                    new_id_name="sample_id", span_policy="partial_strict"):
    """

    Parameters
    ----------
    smalls: pd.DataFrame[begin, end, ...]
        Ex: tokens
    large: pd.DataFrame[begin, end, ...]
        Ex: sentences
    overlap_policy: str or bool
        One of
        - merge_large:
            Keeps small untouched but merges large spans that overlap the same small span
            ex: partition_spans(mentions, sentences) -> merges sentences
        - small_to_leftmost_large:
            Keeps small and large untouched, and assigns small to the leftmost large that overlaps it
            ex: partition_spans(tokens, mentions) -> assign token to the leftmost mention that touches it
        - small_to_rightmost_large:
            Keeps small and large untouched, and assigns small to the rightmost large that overlaps it
            ex: partition_spans(tokens, mentions) -> assign token to the rightmost mention that touches it
        - small_to_biggest_overlap_large:
            keeps small and large untouched, and assigns small to the large span that overlaps it the most
            ex: partition_spans(tokens, mentions) -> assign token to the mention that overlaps it the most
        - False
            do nothing and allow multiple matchings between small and large
    new_id_name: str
        If overlap_policy == "merge_large", this is the column that will host the newly created ids per merge
    span_policy:
        Which policy to use to detect span overlaps

    Returns
    -------

    """

    assert overlap_policy in ("merge_large",
                              "split_small",
                              "small_to_leftmost_large",
                              "small_to_rightmost_large",
                              "small_to_biggest_overlap_large", False), f"Unknown small overlap policy '{overlap_policy}'"

    if not isinstance(smalls, (list, tuple)):
        smalls = [smalls]

    merged_id_cols = doc_id_cols = None
    if overlap_policy == "merge_large":
        original_new_id_name = new_id_name
        while new_id_name in large.columns:
            new_id_name = "_" + new_id_name
        large = large.copy()
        old_to_new = None
        has_created_new_id_col = False
        for small in smalls:
            doc_id_cols, small_id_cols, large_id_cols, small_val_cols, large_val_cols = preprocess_ids(large, small)
            large_id_cols = [c for c in large_id_cols]
            # Merge sentences and mentions
            merged = merge_with_spans(small, large, span_policy=span_policy, how='right', on=[*doc_id_cols, ("begin", "end")])
            # If a mention overlap multiple sentences, assign it to the last sentence
            small_ids = merged[doc_id_cols + small_id_cols].nlstruct.factorize(group_nans=False)
            if has_created_new_id_col:
                large_ids = merged[doc_id_cols + [new_id_name]].nlstruct.factorize(group_nans=False)
            else:
                large_ids = merged[doc_id_cols + large_id_cols].nlstruct.factorize(group_nans=False)
            merged[new_id_name] = make_id_from_merged(
                large_ids,
                small_ids,
                apply_on=[(0, large_ids)])[0]
            merged["begin"] = merged[['begin_x', 'begin_y']].min(axis=1)
            merged["end"] = merged[['end_x', 'end_y']].max(axis=1)
            large = (merged
                     .groupby(new_id_name, as_index=False, observed=True)
                     .agg({**{n: 'first' for n in [*doc_id_cols, *large_id_cols] if n != new_id_name}, 'begin': 'min', 'end': 'max'})
                     .astype({"begin": int, "end": int, **large[doc_id_cols].dtypes}))
            large = large[doc_id_cols + [new_id_name] + ["begin", "end"]]
            large[new_id_name] = large['begin']
            large = large.nlstruct.groupby_assign(doc_id_cols, {new_id_name: lambda x: tuple(np.argsort(np.argsort(x)))})
            old_to_new = large[doc_id_cols + [new_id_name]].drop_duplicates().reset_index(drop=True)
            merged_id_cols = [new_id_name]
        # large[original_new_id_name] = large[doc_id_cols + [new_id_name]].apply(lambda x: "/".join(map(str, x[doc_id_cols])) + "/" + str(x[new_id_name]), axis=1).astype("category")
        # large = large.drop(columns={*doc_id_cols, new_id_name} - {original_new_id_name})
    else:
        original_new_id_name = None
        # merged = merged.drop_duplicates([*doc_id_cols, *small_id_cols], keep=overlap_policy)
        doc_id_cols, small_id_cols, large_id_cols, small_val_cols, large_val_cols = preprocess_ids(large, smalls[0])
        merged_id_cols = large_id_cols
        new_id_name = None
        old_to_new = None

    # Merge sentences and mentions
    new_smalls = []
    for small in smalls:
        doc_id_cols, small_id_cols, large_id_cols, small_val_cols, large_val_cols = preprocess_ids(large, small)
        merged = merge_with_spans(small, large[doc_id_cols + large_id_cols + ['begin', 'end']],
                                  how='inner', span_policy=span_policy, on=[*doc_id_cols, ("begin", "end")])

        if overlap_policy == "small_to_biggest_overlap_large":
            merged = merged.sort_values([*doc_id_cols, *small_id_cols, 'overlap_size_0']).drop_duplicates([*doc_id_cols, *small_id_cols], keep="last")
        elif overlap_policy == "small_to_leftmost_large":
            merged = merged.sort_values([*doc_id_cols, *small_id_cols, 'begin_y']).drop_duplicates([*doc_id_cols, *small_id_cols], keep="first")
        elif overlap_policy == "small_to_rightmost_large":
            merged = merged.sort_values([*doc_id_cols, *small_id_cols, 'begin_y']).drop_duplicates([*doc_id_cols, *small_id_cols], keep="last")
        elif overlap_policy == "split_small":
            merged = merged.assign(begin_x=np.maximum(merged['begin_x'], merged['begin_y']),
                                   end_x=np.minimum(merged['end_x'], merged['end_y']))
        new_small = (
            merged.assign(begin=merged["begin_x"] - merged["begin_y"], end=merged["end_x"] - merged["begin_y"])
                .astype({"begin": int, "end": int})[[*doc_id_cols, *(merged_id_cols or ()), *small_id_cols, *small_val_cols, "begin", "end"]])
        if new_id_name:
            new_small[new_id_name] = new_small[new_id_name].astype(str)
            new_small[new_id_name] = new_small[new_id_name].str.zfill(new_small[new_id_name].str.len().max())
            new_small[original_new_id_name] = join_cols(new_small[doc_id_cols + ([new_id_name] if new_id_name not in doc_id_cols else [])], "/")
            new_small = new_small.drop(columns={*doc_id_cols, new_id_name} - {original_new_id_name})

        new_smalls.append(new_small)

    if original_new_id_name:
        if new_id_name:
            large[new_id_name] = large[new_id_name].astype(str)
            large[new_id_name] = large[new_id_name].str.zfill(large[new_id_name].str.len().max())
            large[original_new_id_name] = join_cols(large[doc_id_cols + [new_id_name]], "/")
            large = large.drop(columns={*doc_id_cols, new_id_name} - {original_new_id_name})
            new_doc_id_cols = [c if c != original_new_id_name else f'_{c}' for c in doc_id_cols]

            old_to_new[new_id_name] = old_to_new[new_id_name].astype(str)
            old_to_new[new_id_name] = old_to_new[new_id_name].str.zfill(old_to_new[new_id_name].str.len().max())
            (old_to_new[original_new_id_name],
             old_to_new[new_doc_id_cols],
             ) = (
                # old_to_new[doc_id_cols + [new_id_name]].apply(lambda x: "/".join(map(str, x[doc_id_cols])) + "/" + str(x[new_id_name]), axis=1),
                join_cols(old_to_new[doc_id_cols + [new_id_name]], "/"),
                old_to_new[doc_id_cols]
            )
            if new_id_name not in (*new_doc_id_cols, original_new_id_name):
                del old_to_new[new_id_name]
        new_smalls = [small.astype({original_new_id_name: large[original_new_id_name].dtype}) for small in new_smalls]
    return new_smalls, large, old_to_new


def join_cols(df, sep="/"):
    df = df.astype(str)
    return reduce(lambda x, y: x + sep + y, (df.iloc[:, i] for i in range(1, len(df.columns))), df.iloc[:, 0])


def split_into_spans(large, small, overlap_policy="split_small", pos_col=None):
    """

    Parameters
    ----------
    large: pd.DataFrame[begin, end, ...]
        Any big span, like a sentence, a mention that needs being cut into pieces
    small: pd.DataFrame[begin, end, ...]
        Any small span that can subdivide a large mention: typically tokens
    overlap_policy: str
        cf partition_spans docstring
        If two large spans overlap the same token, what should we do ?
    pos_col: str
        Column containing the precomputed index of the small spans (=tokens) in a document

    Returns
    -------
    pd.DataFrame
        Large, but with begin and end columns being express in token-units
    """
    if pos_col is None:
        pos_col = next(iter(c for c in small.columns if c.endswith("_pos")))
    [small] = partition_spans([small], large, overlap_policy=overlap_policy)[0]
    doc_id_cols, small_id_cols, large_id_cols, small_val_cols, large_val_cols = preprocess_ids(large, small)
    res = large[[*doc_id_cols, *large_id_cols, *large_val_cols]].merge(
        small
            .eval(f"""
            begin={pos_col}
            end={pos_col} + 1""")
            .groupby(doc_id_cols, as_index=False, observed=True)
            .agg({"begin": "min", "end": "max"}),
        on=doc_id_cols
    )
    return res
