import datetime
import decimal
from typing import Iterable

import numpy as np
from dateutil.parser import parse

from great_expectations.core import ExpectationSuite
from great_expectations.core.expectation_configuration import ExpectationConfiguration
from great_expectations.dataset.util import build_categorical_partition_object
from great_expectations.exceptions import ProfilerError
from great_expectations.profile.base import (
    ProfilerCardinality,
    ProfilerDataType,
    ProfilerTypeMapping,
)
from great_expectations.profile.basic_dataset_profiler import (
    BasicDatasetProfilerBase,
    logger,
)


class UserConfigurableProfiler(BasicDatasetProfilerBase):
    # TODO: Confirm tolerance for every expectation

    # TODO: Figure out how to build in a minimal tolerance when going between pandas and sql (and look into the reason
    #  for this - is it also a float to decimal issue)?
    """
    This profiler helps build strict expectations for the purposes of ensuring
    that two tables are the same.

    config = { "semantic_types":
               {
                "numeric": ["c_acctbal"],
                "string": ["c_address","c_custkey"],
                "value_set": ["c_nationkey","c_mktsegment", 'c_custkey', 'c_name', 'c_address', 'c_phone'],
                },
            "ignored_columns": ignored_columns,
            "excluded_expectations":[],
            "value_set_threshold": "unique"
            "primary_or_compound_key": ["c_name", "c_custkey"],
            "table_expectations_only": False
    }


    Separate suite builder, which takes a dictionary where the keys are columns and the values are the expectations to
    build for those columns. The profiler will populate this dictionary in different ways. It might take a prepopulated
    set of the columns and expectations and add to them.


    The table_profile method returns a dictionary where they keys are domain tuples, and the values are dictionaries of
    expectations with their success_kwargs. After profiling, this profile object can be inspected and edited. Example

    profile = {
                ("user_id,) = { "expect_column_values_to_be_in_set":{
                                                                        "value_set
                                                                    }


                                }
                }

    We need to get a list of ExpectationConfiguration objects and use those to initialize a new suite. We should also
    provide convenience methods to inspect and modify that list of ECs (view by expectation type, view by domain, remove
    individual ECs, remove by column, remove by expectation type.
    """

    def __init__(self, dataset, config=None, tolerance=0):
        self.dataset = dataset
        self.config = config
        self.primary_or_compound_key = []
        self.ignored_columns = []
        self.value_set_threshold = None
        self.table_expectations_only = None
        self.excluded_expectations = []
        self.column_info = {}

        if config is not None:
            self.semantic_type_dict = config.get("semantic_types")
            self.primary_or_compound_key = config.get("primary_or_compound_key") or []
            self.ignored_columns = config.get("ignored_columns") or []
            self.excluded_expectations = config.get("excluded_expectations") or []
            self.value_set_threshold = config.get("value_set_threshold")
            self.table_expectations_only = config.get("table_expectations_only")

            if self.table_expectations_only is True:
                self.ignored_columns = dataset.get_table_columns()
                logger.debug(
                    "table_expectations_only is set to True. Ignoring all columns and creating expectations only \
                           at the table level"
                )

        included_columns = [
            column_name
            for column_name in dataset.get_table_columns()
            if column_name not in self.ignored_columns
        ]
        for column_name in included_columns:
            self._get_column_cardinality_with_caching(dataset, column_name)
            self._add_column_type_and_build_type_expectations(dataset, column_name)
            if config is not None and config.get("semantic_types") is not None:
                self._add_semantic_types_by_column_from_config_to_column_info(
                    dataset, config, column_name
                )

    def build_suite(self, dataset, config=None, tolerance=0):
        if config:
            self._validate_config(config)
            semantic_types = config.get("semantic_types")
            if semantic_types:
                self._validate_semantic_types_dict(dataset=dataset, config=config)
                return self._build_expectation_list_from_config(
                    dataset=dataset, config=config, tolerance=tolerance
                )

        return self._profile_and_build_expectation_list(
            dataset=dataset, config=config, tolerance=tolerance
        )

    def _build_expectation_list_from_config(self, dataset, config, tolerance=0):
        if not self.semantic_type_dict:
            raise ValueError(
                "A config with a semantic_types dict must be included in order to use this profiler."
            )
        self._build_expectations_table(dataset)

        if self.value_set_threshold:
            logger.debug(
                "Using this profiler with a semantic_types dict will ignore the value_set_threshold parameter. If "
                "you would like to include value_set expectations, you can include a 'value_set' entry in your "
                "semantic_types dict with any columns for which you would like a value_set expectation, or you can "
                "remove the semantic_types dict from the config."
            )

        if self.primary_or_compound_key is not None:
            self._build_expectations_primary_or_compound_key(
                dataset, self.primary_or_compound_key
            )

        for column_name, column_info in self.column_info.items():
            semantic_types = column_info.get("semantic_types")
            for semantic_type in semantic_types:
                semantic_type_fn = self.semantic_type_functions.get(semantic_type)
                getattr(self, semantic_type_fn)(dataset, column_name, tolerance)

        for column_name in self.column_info.keys():
            self._build_expectations_for_all_column_types(dataset, column_name)

        expectation_suite = self._build_column_description_metadata(dataset)
        logger.debug("")
        self._display_suite_by_column(suite=expectation_suite)
        return expectation_suite

    def _profile_and_build_expectation_list(self, dataset, config=None, tolerance=0):
        if not self.value_set_threshold:
            self.value_set_threshold = "many"

        if self.primary_or_compound_key:
            self._build_expectations_primary_or_compound_key(
                dataset=dataset, column_list=self.primary_or_compound_key
            )
        self._build_expectations_table(dataset=dataset, tolerance=tolerance)
        for column_name, column_info in self.column_info.items():
            data_type = column_info.get("type")
            cardinality = column_info.get("cardinality")

            if data_type in ("float", "int", "numeric"):
                self._build_expectations_numeric(
                    dataset=dataset,
                    column=column_name,
                    tolerance=tolerance,
                )

            if data_type == "datetime":
                self._build_expectations_datetime(
                    dataset=dataset,
                    column=column_name,
                    tolerance=tolerance,
                )

            if self._cardinality_enumeration.get(
                self.value_set_threshold
            ) >= self._cardinality_enumeration.get(cardinality):
                self._build_expectations_value_set(dataset=dataset, column=column_name)

            self._build_expectations_for_all_column_types(
                dataset=dataset, column=column_name
            )

        expectation_suite = self._build_column_description_metadata(dataset)
        logger.debug("")
        self._display_suite_by_column(
            suite=expectation_suite
        )  # include in the actual profiler
        return expectation_suite

    def _validate_config(self, config):
        config_parameters = {
            "ignored_columns": list,
            "excluded_expectations": list,
            "primary_or_compound_key": list,
            "value_set_threshold": str,
            "semantic_types": dict,
            "table_expectations_only": bool,
        }

        for k, v in config.items():
            assert (
                k in config_parameters
            ), f"Parameter {k} from config is not recognized."
            if v:
                assert isinstance(
                    v, config_parameters.get(k)
                ), f"Config parameter {k} must be formatted as a {config_parameters.get(k)} rather than a {type(v)}."

    def _validate_semantic_types_dict(self, dataset, config):
        semantic_type_dict = config.get("semantic_types")
        if not isinstance(semantic_type_dict, dict):
            raise ValueError(
                f"The semantic_types dict in the config must be a dictionary, but is currently a "
                f"{type(semantic_type_dict)}. Please reformat."
            )
        for k, v in semantic_type_dict.items():
            assert isinstance(v, list), (
                "Entries in semantic type dict must be lists of column names e.g. "
                "{'semantic_types': {'numeric': ['number_of_transactions']}}"
            )
            if k not in self._semantic_types:
                logger.debug(
                    f"{k} is not a recognized semantic_type and will be skipped."
                )

        selected_columns = [
            column
            for column_list in semantic_type_dict.values()
            for column in column_list
        ]
        if selected_columns:
            for column in selected_columns:
                if column not in dataset.get_table_columns():
                    raise ProfilerError(f"Column {column} does not exist.")

        dataset.set_default_expectation_argument("catch_exceptions", False)

        for column_name, column_info in self.column_info.items():
            config_semantic_types = column_info["semantic_types"]
            for semantic_type in config_semantic_types:
                if semantic_type == "datetime":
                    assert column_info.get("type") in (
                        "datetime",
                        "string",
                    ), (  # TODO: Should we allow strings here?
                        f"Column {column_name} must be a datetime column or a string but appears to be "
                        f"{column_info['type']}"
                    )
                elif semantic_type == "numeric":
                    assert column_info["type"] in (
                        "int",
                        "float",
                        "numeric",
                    ), f"Column {column_name} must be an int or a float but appears to be {column_info['type']}"
                elif semantic_type in ("string", "value_set"):
                    pass
                # Should we validate value_set expectations if the cardinality is unexpected? This behavior conflicts
                #  with the compare two tables functionality, which is why I am not including it for now.
                # elif semantic_type in ("boolean", "value_set"):
                #     if column_info["cardinality"] in ("many", "very many", "unique"):
                #         logger.debug(f"Column {column_name} appears to have high cardinality. Creating a "
                #                     f"{semantic_type} expectation, but ensure that this is correctly configured.")
                # else:
                #     logger.debug(f"Semantic_type: {semantic_type} is unknown. Skipping")

    def _add_column_type_and_build_type_expectations(self, dataset, column_name):
        type_expectation_is_excluded = False
        if "expect_column_values_to_be_in_type_list" in self.excluded_expectations:
            type_expectation_is_excluded = True
            logger.debug(
                "expect_column_values_to_be_in_type_list is in the excluded_expectations list. This"
                "expectation is required to establish column data, so it will be run and then removed from the"
                "expectation suite."
            )

        column_info_entry = self.column_info.get(column_name)
        if not column_info_entry:
            column_info_entry = {}
            self.column_info[column_name] = column_info_entry
        column_type = column_info_entry.get("type")
        if not column_type:
            column_type = self._get_column_type(dataset, column_name)
            column_info_entry["type"] = column_type
            if type_expectation_is_excluded:
                # remove the expectation
                dataset.remove_expectation(
                    ExpectationConfiguration(
                        expectation_type="expect_column_values_to_be_in_type_list",
                        kwargs={"column": column_name},
                    )
                )
            dataset.set_config_value("interactive_evaluation", True)

        return column_type

    def _get_column_cardinality_with_caching(self, dataset, column_name):
        column_info_entry = self.column_info.get(column_name)
        if not column_info_entry:
            column_info_entry = {}
            self.column_info[column_name] = column_info_entry
        column_cardinality = column_info_entry.get("cardinality")
        if not column_cardinality:
            column_cardinality = self._get_column_cardinality(dataset, column_name)
            column_info_entry["cardinality"] = column_cardinality
            # remove the expectations
            dataset.remove_expectation(
                ExpectationConfiguration(
                    expectation_type="expect_column_unique_value_count_to_be_between",
                    kwargs={"column": column_name},
                )
            )
            dataset.remove_expectation(
                ExpectationConfiguration(
                    expectation_type="expect_column_proportion_of_unique_values_to_be_between",
                    kwargs={"column": column_name},
                )
            )
            dataset.set_config_value("interactive_evaluation", True)

        return column_cardinality

    def _add_semantic_types_by_column_from_config_to_column_info(
        self, dataset, config, column_name
    ):
        column_info_entry = self.column_info.get(column_name)
        if not column_info_entry:
            column_info_entry = {}
            self.column_info[column_name] = column_info_entry

        semantic_types = column_info_entry.get("semantic_types")

        if not semantic_types:
            assert isinstance(
                self.semantic_type_dict, dict
            ), f"The semantic_types dict in the config must be a dictionary, but is currently a {type(self.semantic_type_dict)}. Please reformat."
            semantic_types = []
            for semantic_type, column_list in self.semantic_type_dict.items():
                if column_name in column_list and semantic_type in self._semantic_types:
                    semantic_types.append(semantic_type)
            column_info_entry["semantic_types"] = semantic_types

        return semantic_types

    def _build_column_description_metadata(self, dataset):
        columns = dataset.get_table_columns()
        expectation_suite = dataset.get_expectation_suite(
            suppress_warnings=True, discard_failed_expectations=False
        )

        meta_columns = {}
        for column in columns:
            meta_columns[column] = {"description": ""}
        if not expectation_suite.meta:
            expectation_suite.meta = {"columns": meta_columns, "notes": {""}}
        else:
            expectation_suite.meta["columns"] = meta_columns

        return expectation_suite

    def _display_suite_by_column(self, suite):
        expectations = suite.expectations
        expectations_by_column = {}
        for expectation in expectations:
            domain = expectation["kwargs"].get("column") or "table_level_expectations"
            if expectations_by_column.get(domain) is None:
                expectations_by_column[domain] = [expectation]
            else:
                expectations_by_column[domain].append(expectation)

        if not expectations_by_column:
            print("No expectations included in suite.")
        else:
            print("Creating an expectation suite with the following expectations:\n")

        if "table_level_expectations" in expectations_by_column:
            table_level_expectations = expectations_by_column.pop(
                "table_level_expectations"
            )
            print("Table-Level Expectations")
            for expectation in sorted(
                table_level_expectations, key=lambda x: x.expectation_type
            ):
                print(expectation.expectation_type)

        if expectations_by_column:
            print("\nExpectations by Column")

        for column in sorted(expectations_by_column):
            info_column = self.column_info.get(column) or {}

            semantic_types = info_column.get("semantic_types")
            type_ = info_column.get("type")
            cardinality = info_column.get("cardinality")

            if semantic_types:
                type_string = f" | Semantic Type: {semantic_types[0] if len(semantic_types)==1 else semantic_types}"
            elif type_:
                type_string = f" | Column Data Type: {type_}"
            else:
                type_string = ""

            if cardinality:
                cardinality_string = f" | Cardinality: {cardinality}"
            else:
                cardinality_string = ""

            column_string = (
                f"Column Name: {column}{type_string or ''}{cardinality_string or ''}"
            )
            print(column_string)

            for expectation in sorted(
                expectations_by_column.get(column), key=lambda x: x.expectation_type
            ):
                print(expectation.expectation_type)
            print("\n")

    def _build_expectations_value_set(self, dataset, column, tolerance=0):
        if "expect_column_values_to_be_in_set" not in self.excluded_expectations:
            value_set = dataset.expect_column_distinct_values_to_be_in_set(
                column, value_set=None, result_format="SUMMARY"
            ).result["observed_value"]

            dataset.remove_expectation(
                ExpectationConfiguration(
                    expectation_type="expect_column_distinct_values_to_be_in_set",
                    kwargs={"column": column},
                ),
                match_type="domain",
            )

            dataset.expect_column_values_to_be_in_set(column, value_set=value_set)

    def _build_expectations_numeric(self, dataset, column, tolerance=0):
        # min
        if "expect_column_min_to_be_between" not in self.excluded_expectations:
            observed_min = dataset.expect_column_min_to_be_between(
                column, min_value=None, max_value=None, result_format="SUMMARY"
            ).result["observed_value"]
            if not self._is_nan(observed_min):
                # places = len(str(observed_min)[str(observed_min).find('.') + 1:])
                # tolerance = 10 ** int(-places)
                # tolerance = float(decimal.Decimal.from_float(float(observed_min)) - decimal.Decimal(str(observed_min)))
                dataset.expect_column_min_to_be_between(
                    column,
                    min_value=observed_min - tolerance,
                    max_value=observed_min + tolerance,
                )

            else:
                dataset.remove_expectation(
                    ExpectationConfiguration(
                        expectation_type="expect_column_min_to_be_between",
                        kwargs={"column": column},
                    ),
                    match_type="domain",
                )
                logger.debug(
                    f"Skipping expect_column_min_to_be_between because observed value is nan: {observed_min}"
                )

        # max
        if "expect_column_max_to_be_between" not in self.excluded_expectations:
            observed_max = dataset.expect_column_max_to_be_between(
                column, min_value=None, max_value=None, result_format="SUMMARY"
            ).result["observed_value"]
            if not self._is_nan(observed_max):
                # tolerance = float(decimal.Decimal.from_float(float(observed_max)) - decimal.Decimal(str(observed_max)))
                dataset.expect_column_max_to_be_between(
                    column,
                    min_value=observed_max - tolerance,
                    max_value=observed_max + tolerance,
                )

            else:
                dataset.remove_expectation(
                    ExpectationConfiguration(
                        expectation_type="expect_column_max_to_be_between",
                        kwargs={"column": column},
                    ),
                    match_type="domain",
                )
                logger.debug(
                    f"Skipping expect_column_max_to_be_between because observed value is nan: {observed_max}"
                )

        # mean
        if "expect_column_mean_to_be_between" not in self.excluded_expectations:
            observed_mean = dataset.expect_column_mean_to_be_between(
                column, min_value=None, max_value=None, result_format="SUMMARY"
            ).result["observed_value"]
            if not self._is_nan(observed_mean):
                # tolerance = float(decimal.Decimal.from_float(float(observed_mean)) - decimal.Decimal(str(observed_mean)))
                dataset.expect_column_mean_to_be_between(
                    column,
                    min_value=observed_mean - tolerance,
                    max_value=observed_mean + tolerance,
                )

            else:
                dataset.remove_expectation(
                    ExpectationConfiguration(
                        expectation_type="expect_column_mean_to_be_between",
                        kwargs={"column": column},
                    ),
                    match_type="domain",
                )
                logger.debug(
                    f"Skipping expect_column_mean_to_be_between because observed value is nan: {observed_mean}"
                )

        # median
        if "expect_column_median_to_be_between" not in self.excluded_expectations:
            observed_median = dataset.expect_column_median_to_be_between(
                column, min_value=None, max_value=None, result_format="SUMMARY"
            ).result["observed_value"]
            if not self._is_nan(observed_median):
                # places = len(str(observed_median)[str(observed_median).find('.') + 1:])
                # tolerance = 10 ** int(-places)
                # tolerance = float(decimal.Decimal.from_float(float(observed_median)) - decimal.Decimal(str(observed_median)))
                dataset.expect_column_median_to_be_between(
                    column,
                    min_value=observed_median - tolerance,
                    max_value=observed_median + tolerance,
                )

            else:
                dataset.remove_expectation(
                    ExpectationConfiguration(
                        expectation_type="expect_column_median_to_be_between",
                        kwargs={"column": column},
                    ),
                    match_type="domain",
                )
                logger.debug(
                    f"Skipping expect_column_median_to_be_between because observed value is nan: {observed_median}"
                )

        # quantile values
        if (
            "expect_column_quantile_values_to_be_between"
            not in self.excluded_expectations
        ):
            allow_relative_error: bool = dataset.attempt_allowing_relative_error()
            quantile_result = dataset.expect_column_quantile_values_to_be_between(
                column,
                quantile_ranges={
                    "quantiles": [0.05, 0.25, 0.5, 0.75, 0.95],
                    "value_ranges": [
                        [None, None],
                        [None, None],
                        [None, None],
                        [None, None],
                        [None, None],
                    ],
                },
                allow_relative_error=allow_relative_error,
                result_format="SUMMARY",
                catch_exceptions=True,
            )
            if quantile_result.exception_info and (
                quantile_result.exception_info["exception_traceback"]
                or quantile_result.exception_info["exception_message"]
            ):
                dataset.remove_expectation(
                    ExpectationConfiguration(
                        expectation_type="expect_column_quantile_values_to_be_between",
                        kwargs={"column": column},
                    ),
                    match_type="domain",
                )
                logger.debug(quantile_result.exception_info["exception_traceback"])
                logger.debug(quantile_result.exception_info["exception_message"])
            else:
                dataset.set_config_value("interactive_evaluation", False)

                dataset.expect_column_quantile_values_to_be_between(
                    column,
                    quantile_ranges={
                        "quantiles": quantile_result.result["observed_value"][
                            "quantiles"
                        ],
                        "value_ranges": [
                            [v - 1, v + 1]
                            for v in quantile_result.result["observed_value"]["values"]
                        ],
                    },
                    allow_relative_error=allow_relative_error,
                    catch_exceptions=True,
                )
                dataset.set_config_value("interactive_evaluation", True)

    def _build_expectations_primary_or_compound_key(self, dataset, column_list):
        # uniqueness
        if (
            len(column_list) > 1
            and "expect_compound_columns_to_be_unique" not in self.excluded_expectations
        ):
            dataset.expect_compound_columns_to_be_unique(column_list)
        elif len(column_list) < 1:
            raise ValueError(
                "When specifying a primary or compound key, column_list must not be empty"
            )
        else:
            [column] = column_list
            if "expect_column_values_to_be_unique" not in self.excluded_expectations:
                dataset.expect_column_values_to_be_unique(column)

    def _build_expectations_string(self, dataset, column, tolerance=0):
        # value_lengths

        if (
            "expect_column_value_lengths_to_be_between"
            not in self.excluded_expectations
        ):
            # With the 0.12 API there isn't a quick way to introspect for value_lengths - if we did that, we could
            #  build a potentially useful value_lengths expectation here.
            pass

    def _build_expectations_datetime(self, dataset, column, tolerance=0):
        if "expect_column_values_to_be_between" not in self.excluded_expectations:
            min_value = dataset.expect_column_min_to_be_between(
                column,
                min_value=None,
                max_value=None,
                parse_strings_as_datetimes=True,
                result_format="SUMMARY",
            ).result["observed_value"]

            if min_value is not None:
                try:
                    min_value = min_value + datetime.timedelta(days=-365 * tolerance)
                except OverflowError:
                    min_value = datetime.datetime.min
                except TypeError:
                    min_value = parse(min_value) + datetime.timedelta(
                        days=(-365 * tolerance)
                    )

            dataset.remove_expectation(
                ExpectationConfiguration(
                    expectation_type="expect_column_min_to_be_between",
                    kwargs={"column": column},
                ),
                match_type="domain",
            )

            max_value = dataset.expect_column_max_to_be_between(
                column,
                min_value=None,
                max_value=None,
                parse_strings_as_datetimes=True,
                result_format="SUMMARY",
            ).result["observed_value"]
            if max_value is not None:
                try:
                    max_value = max_value + datetime.timedelta(days=(365 * tolerance))
                except OverflowError:
                    max_value = datetime.datetime.max
                except TypeError:
                    max_value = parse(max_value) + datetime.timedelta(
                        days=(365 * tolerance)
                    )

            dataset.remove_expectation(
                ExpectationConfiguration(
                    expectation_type="expect_column_max_to_be_between",
                    kwargs={"column": column},
                ),
                match_type="domain",
            )
            if min_value is not None or max_value is not None:
                dataset.expect_column_values_to_be_between(
                    column,
                    min_value=min_value,
                    max_value=max_value,
                    parse_strings_as_datetimes=True,
                )

    def _build_expectations_for_all_column_types(self, dataset, column, tolerance=0):
        if "expect_column_values_to_not_be_null" not in self.excluded_expectations:
            not_null_result = dataset.expect_column_values_to_not_be_null(column)
            if not not_null_result.success:
                unexpected_percent = float(not_null_result.result["unexpected_percent"])
                if unexpected_percent >= 50:
                    potential_mostly_value = (unexpected_percent + tolerance) / 100.0
                    safe_mostly_value = round(potential_mostly_value, 3)
                    dataset.remove_expectation(
                        ExpectationConfiguration(
                            expectation_type="expect_column_values_to_not_be_null",
                            kwargs={"column": column},
                        ),
                        match_type="domain",
                    )
                    if (
                        "expect_column_values_to_be_null"
                        not in self.excluded_expectations
                    ):
                        dataset.expect_column_values_to_be_null(
                            column, mostly=safe_mostly_value
                        )
                else:
                    potential_mostly_value = (
                        100.0 - unexpected_percent - tolerance
                    ) / 100.0
                    safe_mostly_value = round(max(0.001, potential_mostly_value), 3)
                    dataset.expect_column_values_to_not_be_null(
                        column, mostly=safe_mostly_value
                    )
        if (
            "expect_column_proportion_of_unique_values_to_be_between"
            not in self.excluded_expectations
        ):
            pct_unique = (
                dataset.expect_column_proportion_of_unique_values_to_be_between(
                    column, None, None
                ).result["observed_value"]
            )

            if not self._is_nan(pct_unique):
                dataset.expect_column_proportion_of_unique_values_to_be_between(
                    column, min_value=pct_unique, max_value=pct_unique
                )
            else:
                dataset.remove_expectation(
                    ExpectationConfiguration(
                        expectation_type="expect_column_proportion_of_unique_values_to_be_between",
                        kwargs={"column": column},
                    ),
                    match_type="domain",
                )

                logger.debug(
                    f"Skipping expect_column_proportion_of_unique_values_to_be_between because observed value is nan: {pct_unique}"
                )

    def _build_expectations_table(self, dataset, tolerance=0):
        if (
            "expect_table_columns_to_match_ordered_list"
            not in self.excluded_expectations
        ):
            columns = dataset.get_table_columns()
            dataset.expect_table_columns_to_match_ordered_list(columns)

        if "expect_table_row_count_to_be_between" not in self.excluded_expectations:
            row_count = dataset.expect_table_row_count_to_be_between(
                min_value=0, max_value=None
            ).result["observed_value"]
            min_value = max(0, int(row_count * (1 - tolerance)))
            max_value = int(row_count * (1 + tolerance))

            dataset.expect_table_row_count_to_be_between(
                min_value=min_value, max_value=max_value
            )

    def _get_column_type(self, df, column):

        # list of types is used to support pandas and sqlalchemy
        type_ = None
        df.set_config_value("interactive_evaluation", True)
        try:

            if (
                df.expect_column_values_to_be_in_type_list(
                    column, type_list=sorted(list(ProfilerTypeMapping.INT_TYPE_NAMES))
                ).success
                and df.expect_column_values_to_be_in_type_list(
                    column, type_list=sorted(list(ProfilerTypeMapping.FLOAT_TYPE_NAMES))
                ).success
            ):
                type_ = "numeric"

            elif df.expect_column_values_to_be_in_type_list(
                column, type_list=sorted(list(ProfilerTypeMapping.INT_TYPE_NAMES))
            ).success:
                type_ = "int"

            elif df.expect_column_values_to_be_in_type_list(
                column, type_list=sorted(list(ProfilerTypeMapping.FLOAT_TYPE_NAMES))
            ).success:
                type_ = "float"

            elif df.expect_column_values_to_be_in_type_list(
                column, type_list=sorted(list(ProfilerTypeMapping.STRING_TYPE_NAMES))
            ).success:
                type_ = "string"

            elif df.expect_column_values_to_be_in_type_list(
                column, type_list=sorted(list(ProfilerTypeMapping.BOOLEAN_TYPE_NAMES))
            ).success:
                type_ = "boolean"

            elif df.expect_column_values_to_be_in_type_list(
                column, type_list=sorted(list(ProfilerTypeMapping.DATETIME_TYPE_NAMES))
            ).success:
                type_ = "datetime"

            else:
                df.expect_column_values_to_be_in_type_list(column, type_list=None)
                type_ = "unknown"
        except NotImplementedError:
            type_ = "unknown"

        if type_ == "numeric":
            df.expect_column_values_to_be_in_type_list(
                column,
                type_list=sorted(list(ProfilerTypeMapping.INT_TYPE_NAMES))
                + sorted(list(ProfilerTypeMapping.FLOAT_TYPE_NAMES)),
            )

        df.set_config_value("interactive_evaluation", False)
        return type_

    def _get_column_cardinality(self, df, column):
        num_unique = None
        pct_unique = None
        df.set_config_value("interactive_evaluation", True)

        try:
            num_unique = df.expect_column_unique_value_count_to_be_between(
                column, None, None
            ).result["observed_value"]
            pct_unique = df.expect_column_proportion_of_unique_values_to_be_between(
                column, None, None
            ).result["observed_value"]
        except KeyError:  # if observed_value value is not set
            logger.error(
                "Failed to get cardinality of column {:s} - continuing...".format(
                    column
                )
            )
        # Previously, if we had 25 possible categories out of 1000 rows, this would comes up as many, because of its
        #  percentage, so it was tweaked here, but is still experimental.
        if num_unique is None or num_unique == 0 or pct_unique is None:
            cardinality = "none"
        elif pct_unique == 1.0:
            cardinality = "unique"
        elif num_unique == 1:
            cardinality = "one"
        elif num_unique == 2:
            cardinality = "two"
        elif num_unique < 20:
            cardinality = "very_few"
        elif num_unique < 60:
            cardinality = "few"
        elif pct_unique > 0.1:
            cardinality = "very_many"
        else:
            cardinality = "many"

        df.set_config_value("interactive_evaluation", False)

        return cardinality

    def _is_nan(self, value):
        try:
            return np.isnan(value)
        except TypeError:
            return False

    semantic_type_functions = {
        "datetime": "_build_expectations_datetime",
        "numeric": "_build_expectations_numeric",
        "string": "_build_expectations_string",
        "value_set": "_build_expectations_value_set",
        "boolean": "_build_expectations_value_set",
        "other": "_build_expectations_for_all_column_types",
    }

    _cardinality_enumeration = {
        "none": 0,
        "one": 1,
        "two": 2,
        "very_few": 3,
        "few": 4,
        "many": 5,
        "very_many": 6,
        "unique": 7,
    }
    _semantic_types = {
        "datetime",
        "numeric",
        "string",
        "value_set",
        "boolean",
        "other",
    }