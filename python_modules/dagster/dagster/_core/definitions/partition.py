import copy
import hashlib
import inspect
import json
from abc import ABC, abstractmethod
from datetime import (
    datetime,
    time,
    timedelta,
)
from enum import Enum
from typing import (
    Any,
    Callable,
    Generic,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Type,
    TypeVar,
    Union,
    cast,
)

import pendulum
from dateutil.relativedelta import relativedelta
from typing_extensions import TypeAlias

import dagster._check as check
from dagster._annotations import PublicAttr, public
from dagster._core.definitions.partition_key_range import PartitionKeyRange
from dagster._core.definitions.target import ExecutableDefinition
from dagster._core.instance import DagsterInstance, DynamicPartitionsStore
from dagster._core.storage.tags import PARTITION_NAME_TAG
from dagster._serdes import whitelist_for_serdes
from dagster._seven.compat.pendulum import PendulumDateTime, to_timezone
from dagster._utils import frozenlist
from dagster._utils.backcompat import deprecation_warning, experimental_arg_warning
from dagster._utils.merger import merge_dicts
from dagster._utils.schedules import schedule_execution_time_iterator

from ..decorator_utils import get_function_params
from ..errors import (
    DagsterInvalidDefinitionError,
    DagsterInvalidDeserializationVersionError,
    DagsterInvalidInvocationError,
    DagsterInvariantViolationError,
    DagsterUnknownPartitionError,
    ScheduleExecutionError,
    user_code_error_boundary,
)
from ..storage.pipeline_run import DagsterRun
from .config import ConfigMapping
from .mode import DEFAULT_MODE_NAME
from .run_request import RunRequest, SkipReason
from .schedule_definition import (
    DefaultScheduleStatus,
    ScheduleDefinition,
    ScheduleEvaluationContext,
    ScheduleExecutionFunction,
    ScheduleRunConfigFunction,
    ScheduleShouldExecuteFunction,
    ScheduleTagsFunction,
)
from .utils import check_valid_name, validate_tags

DEFAULT_DATE_FORMAT = "%Y-%m-%d"

T = TypeVar("T")


RawPartitionFunction: TypeAlias = Union[
    Callable[[Optional[datetime]], Sequence[Union[str, "Partition[T]"]]],
    Callable[[], Sequence[Union[str, "Partition[T]"]]],
]

PartitionFunction: TypeAlias = Callable[[Optional[datetime]], Sequence["Partition[Any]"]]
PartitionTagsFunction: TypeAlias = Callable[["Partition[object]"], Mapping[str, str]]
PartitionScheduleFunction: TypeAlias = Callable[[datetime], Mapping[str, Any]]
PartitionSelectorFunction: TypeAlias = Callable[
    [ScheduleEvaluationContext, "PartitionSetDefinition[T]"],
    Union["Partition[T]", Sequence["Partition[T]"], SkipReason],
]

# Dagit selects partition ranges following the format '2022-01-13...2022-01-14'
# "..." is an invalid substring in partition keys
# The other escape characters are characters that may not display in Dagit
INVALID_PARTITION_SUBSTRINGS = ["...", "\a", "\b", "\f", "\n", "\r", "\t", "\v", "\0"]


class Partition(Generic[T]):
    """
    A Partition represents a single slice of the entire set of a job's possible work. It consists
    of a value, which is an object that represents that partition, and an optional name, which is
    used to label the partition in a human-readable way.

    Args:
        value (Any): The object for this partition
        name (str): Name for this partition
    """

    def __init__(self, value: T, name: Optional[str] = None):
        self._value = value
        self._name = check.str_param(name or str(value), "name")

    @property
    def value(self) -> T:
        return self._value

    @property
    def name(self) -> str:
        return self._name

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Partition):
            return False
        else:
            other = cast(Partition[object], other)
            return self.value == other.value and self.name == other.name


def schedule_partition_range(
    start: datetime,
    end: Optional[datetime],
    cron_schedule: str,
    fmt: str,
    timezone: Optional[str],
    execution_time_to_partition_fn: Callable[[datetime], datetime],
    current_time: Optional[datetime],
) -> Sequence[Partition[datetime]]:
    if end and start > end:
        raise DagsterInvariantViolationError(
            'Selected date range start "{start}" is after date range end "{end}'.format(
                start=start.strftime(fmt),
                end=end.strftime(fmt),
            )
        )

    tz = timezone if timezone else "UTC"

    _current_time = current_time if current_time else pendulum.now(tz)

    # Coerce to the definition timezone
    _start = (
        to_timezone(start, tz)
        if isinstance(start, PendulumDateTime)
        else pendulum.instance(start, tz=tz)
    )
    _current_time = (
        to_timezone(_current_time, tz)
        if isinstance(_current_time, PendulumDateTime)
        else pendulum.instance(_current_time, tz=tz)
    )

    # The end partition time should be before the last partition that
    # executes before the current time
    end_partition_time = execution_time_to_partition_fn(_current_time)

    # The partition set has an explicit end time that represents the end of the partition range
    if end:
        _end = (
            to_timezone(end, tz)
            if isinstance(end, PendulumDateTime)
            else pendulum.instance(end, tz=tz)
        )

        # If the explicit end time is before the last partition time,
        # update the end partition time
        end_partition_time = min(_end, end_partition_time)

    end_timestamp = end_partition_time.timestamp()

    partitions: List[Partition[datetime]] = []
    for next_time in schedule_execution_time_iterator(_start.timestamp(), cron_schedule, tz):
        partition_time = execution_time_to_partition_fn(next_time)

        if partition_time.timestamp() > end_timestamp:
            break

        if partition_time.timestamp() < _start.timestamp():
            continue

        partitions.append(Partition(value=partition_time, name=partition_time.strftime(fmt)))

    return partitions


@whitelist_for_serdes
class ScheduleType(Enum):
    HOURLY = "HOURLY"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"

    @property
    def ordinal(self):
        return {"HOURLY": 1, "DAILY": 2, "WEEKLY": 3, "MONTHLY": 4}[self.value]

    @property
    def delta(self):
        if self == ScheduleType.HOURLY:
            return timedelta(hours=1)
        elif self == ScheduleType.DAILY:
            return timedelta(days=1)
        elif self == ScheduleType.WEEKLY:
            return timedelta(weeks=1)
        elif self == ScheduleType.MONTHLY:
            return relativedelta(months=1)
        else:
            check.failed(f"Unexpected ScheduleType {self}")

    def __gt__(self, other: "ScheduleType") -> bool:
        check.inst(other, ScheduleType, "Cannot compare ScheduleType with non-ScheduleType")
        return self.ordinal > other.ordinal

    def __lt__(self, other: "ScheduleType") -> bool:
        check.inst(other, ScheduleType, "Cannot compare ScheduleType with non-ScheduleType")
        return self.ordinal < other.ordinal


class PartitionsDefinition(ABC, Generic[T]):
    """
    Defines a set of partitions, which can be attached to a software-defined asset or job.

    Abstract class with implementations for different kinds of partitions.
    """

    @property
    def partitions_subset_class(self) -> Type["PartitionsSubset"]:
        return DefaultPartitionsSubset

    @abstractmethod
    def get_partitions(
        self,
        current_time: Optional[datetime] = None,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> Sequence[Partition[T]]:
        ...

    def __str__(self) -> str:
        joined_keys = ", ".join([f"'{key}'" for key in self.get_partition_keys()])
        return joined_keys

    @public
    def get_partition_keys(
        self,
        current_time: Optional[datetime] = None,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> Sequence[str]:
        return [
            partition.name
            for partition in self.get_partitions(current_time, dynamic_partitions_store)
        ]

    def get_last_partition_key(
        self,
        current_time: Optional[datetime] = None,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> Optional[str]:
        partitions = self.get_partitions(current_time, dynamic_partitions_store)
        if partitions:
            return partitions[-1].name
        else:
            return None

    def get_first_partition_key(
        self,
        current_time: Optional[datetime] = None,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> Optional[str]:
        partitions = self.get_partitions(current_time, dynamic_partitions_store)
        if partitions:
            return partitions[0].name
        else:
            return None

    def get_partition_keys_in_range(
        self,
        partition_key_range: PartitionKeyRange,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> Sequence[str]:
        partition_keys = self.get_partition_keys(dynamic_partitions_store=dynamic_partitions_store)

        keys_exist = {
            partition_key_range.start: partition_key_range.start in partition_keys,
            partition_key_range.end: partition_key_range.end in partition_keys,
        }
        if not all(keys_exist.values()):
            raise DagsterInvalidInvocationError(
                f"""Partition range {partition_key_range.start} to {partition_key_range.end} is
                not a valid range. Nonexistent partition keys:
                {list(key for key in keys_exist if keys_exist[key] is False)}"""
            )

        return partition_keys[
            partition_keys.index(partition_key_range.start) : partition_keys.index(
                partition_key_range.end
            )
            + 1
        ]

    def empty_subset(self) -> "PartitionsSubset":
        return self.partitions_subset_class.empty_subset(self)

    def subset_with_partition_keys(self, partition_keys: Iterable[str]) -> "PartitionsSubset":
        return self.empty_subset().with_partition_keys(partition_keys)

    def subset_with_all_partitions(
        self,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> "PartitionsSubset":
        return self.subset_with_partition_keys(
            self.get_partition_keys(dynamic_partitions_store=dynamic_partitions_store)
        )

    def deserialize_subset(self, serialized: str) -> "PartitionsSubset":
        return self.partitions_subset_class.from_serialized(self, serialized)

    def can_deserialize_subset(
        self,
        serialized: str,
        serialized_partitions_def_unique_id: Optional[str],
        serialized_partitions_def_class_name: Optional[str],
    ) -> bool:
        return self.partitions_subset_class.can_deserialize(
            self,
            serialized,
            serialized_partitions_def_unique_id,
            serialized_partitions_def_class_name,
        )

    @property
    def serializable_unique_identifier(self) -> str:
        return hashlib.sha1(json.dumps(self.get_partition_keys()).encode("utf-8")).hexdigest()

    def get_tags_for_partition_key(self, partition_key: str) -> Mapping[str, str]:
        tags = {PARTITION_NAME_TAG: partition_key}
        return tags

    def get_num_partitions(
        self,
        current_time: Optional[datetime] = None,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> int:
        return len(self.get_partition_keys(current_time, dynamic_partitions_store))


def raise_error_on_invalid_partition_key_substring(partition_keys: Sequence[str]) -> None:
    for partition_key in partition_keys:
        found_invalid_substrs = [
            invalid_substr
            for invalid_substr in INVALID_PARTITION_SUBSTRINGS
            if invalid_substr in partition_key
        ]
        if found_invalid_substrs:
            raise DagsterInvalidDefinitionError(
                f"{found_invalid_substrs} are invalid substrings in a partition key"
            )


class StaticPartitionsDefinition(
    PartitionsDefinition[str]
):  # pylint: disable=unsubscriptable-object
    """
    A statically-defined set of partitions.

    Example:
        .. code-block:: python

            from dagster import StaticPartitionsDefinition, asset

            oceans_partitions_def = StaticPartitionsDefinition(
                ["arctic", "atlantic", "indian", "pacific", "southern"]
            )

            @asset(partitions_def=oceans_partitions_defs)
            def ml_model_for_each_ocean():
                ...
    """

    def __init__(self, partition_keys: Sequence[str]):
        check.sequence_param(partition_keys, "partition_keys", of_type=str)

        raise_error_on_invalid_partition_key_substring(partition_keys)

        self._partitions = [Partition(key) for key in partition_keys]

    def get_partitions(
        self,
        current_time: Optional[datetime] = None,  # pylint: disable=unused-argument
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> Sequence[Partition[str]]:
        return self._partitions

    def __hash__(self):
        return hash(self.__repr__())

    def __eq__(self, other) -> bool:
        return isinstance(other, StaticPartitionsDefinition) and (
            self is other or self._partitions == other.get_partitions()
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(partition_keys={[p.name for p in self._partitions]})"


class ScheduleTimeBasedPartitionsDefinition(
    PartitionsDefinition[datetime],  # pylint: disable=unsubscriptable-object
    NamedTuple(
        "_ScheduleTimeBasedPartitionsDefinition",
        [
            ("schedule_type", ScheduleType),
            ("start", datetime),
            ("execution_time", time),
            ("execution_day", Optional[int]),
            ("end", Optional[datetime]),
            ("fmt", str),
            ("timezone", str),
            ("offset", int),
        ],
    ),
):
    """Computes the partitions backwards from the scheduled execution times."""

    def __new__(  # pylint: disable=arguments-differ
        cls,
        schedule_type: ScheduleType,
        start: datetime,
        execution_time: Optional[time] = None,
        execution_day: Optional[int] = None,
        end: Optional[datetime] = None,
        fmt: Optional[str] = None,
        timezone: Optional[str] = None,
        offset: Optional[int] = None,
    ):
        if end is not None:
            check.invariant(
                start <= end,
                f'Selected date range start "{start}" is after date range end "{end}"'.format(
                    start=start.strftime(fmt) if fmt is not None else start,
                    end=cast(datetime, end).strftime(fmt) if fmt is not None else end,
                ),
            )
        if schedule_type in [ScheduleType.HOURLY, ScheduleType.DAILY]:
            check.invariant(
                not execution_day,
                f'Execution day should not be provided for schedule type "{schedule_type}"',
            )
        elif schedule_type is ScheduleType.WEEKLY:
            execution_day = execution_day if execution_day is not None else 0
            check.invariant(
                execution_day is not None and 0 <= execution_day <= 6,
                (
                    f'Execution day "{execution_day}" must be between 0 and 6 for '
                    f'schedule type "{schedule_type}"'
                ),
            )
        elif schedule_type is ScheduleType.MONTHLY:
            execution_day = execution_day if execution_day is not None else 1
            check.invariant(
                execution_day is not None and 1 <= execution_day <= 31,
                (
                    f'Execution day "{execution_day}" must be between 1 and 31 for '
                    f'schedule type "{schedule_type}"'
                ),
            )

        return super(ScheduleTimeBasedPartitionsDefinition, cls).__new__(
            cls,
            check.inst_param(schedule_type, "schedule_type", ScheduleType),
            check.inst_param(start, "start", datetime),
            check.opt_inst_param(execution_time, "execution_time", time, time(0, 0)),
            check.opt_int_param(
                execution_day,
                "execution_day",
            ),
            check.opt_inst_param(end, "end", datetime),
            check.opt_str_param(fmt, "fmt", default=DEFAULT_DATE_FORMAT),
            check.opt_str_param(timezone, "timezone", default="UTC"),
            check.opt_int_param(offset, "offset", default=1),
        )

    def get_partitions(
        self,
        current_time: Optional[datetime] = None,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> Sequence[Partition[datetime]]:
        check.opt_inst_param(current_time, "current_time", datetime)

        return schedule_partition_range(
            start=self.start,
            end=self.end,
            cron_schedule=self.get_cron_schedule(),
            fmt=self.fmt,
            timezone=self.timezone,
            execution_time_to_partition_fn=self.get_execution_time_to_partition_fn(),
            current_time=current_time,
        )

    def get_cron_schedule(self) -> str:
        return cron_schedule_from_schedule_type_and_offsets(
            schedule_type=self.schedule_type,
            minute_offset=self.execution_time.minute,
            hour_offset=self.execution_time.hour,
            day_offset=self.execution_day,
        )

    def get_execution_time_to_partition_fn(self) -> Callable[[datetime], datetime]:
        if self.schedule_type is ScheduleType.HOURLY:
            # Using subtract(minutes=d.minute) here instead of .replace(minute=0) because on
            # pendulum 1, replace(minute=0) sometimes changes the timezone:
            # >>> a = create_pendulum_time(2021, 11, 7, 0, 0, tz="US/Central")
            #
            # >>> a.add(hours=1)
            # <Pendulum [2021-11-07T01:00:00-05:00]>
            # >>> a.add(hours=1).replace(minute=0)
            # <Pendulum [2021-11-07T01:00:00-06:00]>
            return lambda d: pendulum.instance(d).subtract(hours=self.offset, minutes=d.minute)
        elif self.schedule_type is ScheduleType.DAILY:
            return (
                lambda d: pendulum.instance(d).replace(hour=0, minute=0).subtract(days=self.offset)
            )
        elif self.schedule_type is ScheduleType.WEEKLY:
            execution_day = cast(int, self.execution_day)
            day_difference = (execution_day - (self.start.weekday() + 1)) % 7
            return (
                lambda d: pendulum.instance(d)
                .replace(hour=0, minute=0)
                .subtract(
                    weeks=self.offset,
                    days=day_difference,
                )
            )
        elif self.schedule_type is ScheduleType.MONTHLY:
            execution_day = cast(int, self.execution_day)
            return (
                lambda d: pendulum.instance(d)
                .replace(hour=0, minute=0)
                .subtract(months=self.offset, days=execution_day - 1)
            )
        else:
            check.assert_never(self.schedule_type)


class DynamicPartitionsDefinition(
    PartitionsDefinition,
    NamedTuple(
        "_DynamicPartitionsDefinition",
        [
            (
                "partition_fn",
                PublicAttr[
                    Optional[
                        Callable[[Optional[datetime]], Union[Sequence[Partition], Sequence[str]]]
                    ]
                ],
            ),
            ("name", PublicAttr[Optional[str]]),
        ],
    ),
):
    """
    A partitions definition whose partition keys can be dynamically added and removed.

    This is useful for cases where the set of partitions is not known at definition time,
    but is instead determined at runtime.

    Partitions can be added and removed using the `add_partitions` and `remove_partitions` methods.

    Args:
        name (Optional[str]): (Experimental) The name of the partitions definition.
        partition_fn (Optional[Callable[[Optional[datetime]], Union[Sequence[Partition], Sequence[str]]]]):
            A function that returns the current set of partitions. This argument is deprecated and
            will be removed in 2.0.0.

    Examples:
        .. code-block:: python

            foo = DynamicPartitionsDefinition(name="foo")

            @sensor(job=my_job)
            def my_sensor(context):
                foo.add_partitions([partition_key], instance=context.instance)
                return my_job.run_request_for_partition(partition_key, instance=context.instance)

    """

    def __new__(  # pylint: disable=arguments-differ
        cls,
        partition_fn: Optional[
            Callable[[Optional[datetime]], Union[Sequence[Partition], Sequence[str]]]
        ] = None,
        name: Optional[str] = None,
    ):
        partition_fn = check.opt_callable_param(partition_fn, "partition_fn")
        name = check.opt_str_param(name, "name")

        if name:
            experimental_arg_warning("name", "DynamicPartitionsDefinition.__new__")

        if partition_fn:
            deprecation_warning(
                "partition_fn", "2.0.0", "Provide partition definition name instead."
            )

        if partition_fn is None and name is None:
            raise DagsterInvalidDefinitionError(
                "Must provide either partition_fn or name to DynamicPartitionsDefinition."
            )

        if partition_fn and name:
            raise DagsterInvalidDefinitionError(
                "Cannot provide both partition_fn and name to DynamicPartitionsDefinition."
            )

        return super(DynamicPartitionsDefinition, cls).__new__(
            cls,
            partition_fn=check.opt_callable_param(partition_fn, "partition_fn"),
            name=check.opt_str_param(name, "name"),
        )

    def _validated_name(self) -> str:
        if self.name is None:
            check.failed(
                "Dynamic partitions definition must have a name to fetch dynamic partitions"
            )
        return self.name

    def __eq__(self, other):
        return (
            isinstance(other, DynamicPartitionsDefinition)
            and self.name == other.name
            and self.partition_fn == other.partition_fn
        )

    def __hash__(self):
        return hash(tuple(self.__repr__()))

    def __str__(self) -> str:
        if self.name:
            return f"Dynamic partitions definition {self._validated_name()}"
        else:
            return super().__str__()

    @property
    def serializable_unique_identifier(self) -> str:
        if not self.name:
            return super().serializable_unique_identifier

        return hashlib.sha1(self.__repr__().encode("utf-8")).hexdigest()

    def get_partitions(
        self,
        current_time: Optional[datetime] = None,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> Sequence[Partition]:
        if self.partition_fn:
            partitions = self.partition_fn(current_time)
            if all(isinstance(partition, Partition) for partition in partitions):
                return cast(Sequence[Partition], partitions)
            else:
                return [Partition(p) for p in partitions]
        else:
            check.opt_inst_param(
                dynamic_partitions_store, "dynamic_partitions_store", DynamicPartitionsStore
            )

            if dynamic_partitions_store is None:
                check.failed(
                    "The instance is not available to load partitions. You may be seeing this error"
                    " when using dynamic partitions with a version of dagit or dagster-cloud that"
                    " is older than 1.1.18."
                )

            partitions = dynamic_partitions_store.get_dynamic_partitions(
                partitions_def_name=self._validated_name()
            )
            return [Partition(key) for key in partitions]

    def add_partitions(self, partition_keys: Sequence[str], instance: DagsterInstance) -> None:
        """
        Add partitions to the specified partition definition.
        Does not add any partitions that already exist.
        """
        check.sequence_param(partition_keys, "partition_keys", of_type=str)
        check.inst_param(instance, "instance", DagsterInstance)

        instance.add_dynamic_partitions(self._validated_name(), partition_keys)

    def has_partition(self, partition_key: str, instance: DagsterInstance) -> bool:
        """
        Checks if a partition key exists for the partitions definition.
        """
        check.str_param(partition_key, "partition_key")
        check.inst_param(instance, "instance", DagsterInstance)
        return instance.has_dynamic_partition(self._validated_name(), partition_key)

    def delete_partition(self, partition_key: str, instance: DagsterInstance) -> None:
        """
        Delete a partition for the specified partition definition.
        If the partition does not exist, exits silently.
        """
        check.str_param(partition_key, "partition_key")
        check.inst_param(instance, "instance", DagsterInstance)
        instance.delete_dynamic_partition(self._validated_name(), partition_key)


class PartitionSetDefinition(Generic[T]):
    """
    Defines a partition set, representing the set of slices making up an axis of a pipeline.

    Args:
        name (str): Name for this partition set
        pipeline_name (str): The name of the pipeline definition
        partition_fn (Optional[Callable[void, Sequence[Partition]]]): User-provided function to define
            the set of valid partition objects.
        solid_selection (Optional[Sequence[str]]): A list of solid subselection (including single
            solid names) to execute with this partition. e.g. ``['*some_solid+', 'other_solid']``
        mode (Optional[str]): The mode to apply when executing this partition. (default: 'default')
        run_config_fn_for_partition (Callable[[Partition], Any]): A
            function that takes a :py:class:`~dagster.Partition` and returns the run
            configuration that parameterizes the execution for this partition.
        tags_fn_for_partition (Callable[[Partition], Optional[dict[str, str]]]): A function that
            takes a :py:class:`~dagster.Partition` and returns a list of key value pairs that will
            be added to the generated run for this partition.
        partitions_def (Optional[PartitionsDefinition]): A set of parameters used to construct the set
            of valid partition objects.
    """

    _name: str
    _pipeline_name: Optional[str]
    _job_name: Optional[str]
    _solid_selection: Optional[Sequence[str]]
    _mode: Optional[str]
    _user_defined_run_config_fn_for_partition: Callable[[Partition], Mapping[str, Any]]
    _user_defined_tags_fn_for_partition: Callable[[Partition], Optional[Mapping[str, str]]]
    _partitions_def: PartitionsDefinition

    def __init__(
        self,
        name: str,
        pipeline_name: Optional[str] = None,
        partition_fn: Optional[RawPartitionFunction] = None,
        solid_selection: Optional[Sequence[str]] = None,
        mode: Optional[str] = None,
        run_config_fn_for_partition: Callable[
            [Partition[T]], Mapping[str, Any]
        ] = lambda _partition: {},
        tags_fn_for_partition: Callable[
            [Partition[T]], Optional[Mapping[str, str]]
        ] = lambda _partition: {},
        partitions_def: Optional[
            PartitionsDefinition[T]  # pylint: disable=unsubscriptable-object
        ] = None,
        job_name: Optional[str] = None,
    ):
        check.invariant(
            not (partition_fn and partitions_def),
            "Only one of `partition_fn` or `partitions_def` must be supplied.",
        )
        check.invariant(
            (pipeline_name or job_name) and not (pipeline_name and job_name),
            "Exactly one one of `job_name` and `pipeline_name` must be supplied.",
        )

        self._name = check_valid_name(name)
        self._pipeline_name = check.opt_str_param(pipeline_name, "pipeline_name")
        self._job_name = check.opt_str_param(job_name, "job_name")
        self._solid_selection = check.opt_nullable_sequence_param(
            solid_selection, "solid_selection", of_type=str
        )
        self._mode = check.opt_str_param(mode, "mode", DEFAULT_MODE_NAME)
        # Type ignores workaround for mypy bug "cannot assign to a method"
        self._user_defined_run_config_fn_for_partition = check.callable_param(
            run_config_fn_for_partition, "run_config_fn_for_partition"
        )
        self._user_defined_tags_fn_for_partition = check.callable_param(
            tags_fn_for_partition, "tags_fn_for_partition"
        )
        if partitions_def is not None:
            self._partitions_def = check.inst_param(
                partitions_def, "partitions_def", PartitionsDefinition
            )
        elif partition_fn is not None:
            _wrapped = self._wrap_partition_fn(partition_fn)
            self._partitions_def = DynamicPartitionsDefinition(partition_fn=_wrapped)
        else:
            check.failed(
                "One of `partition_fn` or `partitions_def` must be supplied.",
            )

    def _wrap_partition_fn(self, partition_fn: RawPartitionFunction) -> PartitionFunction:
        partition_fn_param_count = len(inspect.signature(partition_fn).parameters)

        def wrap_partition(x: Union[str, Partition]) -> Partition:
            if isinstance(x, Partition):
                return x
            if isinstance(x, str):
                return Partition(x)
            raise DagsterInvalidDefinitionError(
                "Expected <Partition> | <str>, received {type}".format(type=type(x))
            )

        def wrapper(current_time: Optional[datetime] = None) -> Sequence[Partition]:
            if not current_time:
                current_time = pendulum.now("UTC")

            check.callable_param(partition_fn, "partition_fn")

            if partition_fn_param_count == 1:
                obj_list = cast(
                    Callable[..., Sequence[Union[Partition[T], str]]],
                    partition_fn,
                )(current_time)
            else:
                obj_list = partition_fn()  # type: ignore

            return [wrap_partition(obj) for obj in obj_list]

        return wrapper

    @property
    def name(self) -> str:
        return self._name

    @property
    def pipeline_name(self) -> Optional[str]:
        return self._pipeline_name

    @property
    def job_name(self) -> Optional[str]:
        return self._job_name

    @property
    def pipeline_or_job_name(self) -> str:
        # one is guaranteed to be set
        return cast(str, self._pipeline_name or self._job_name)

    @property
    def solid_selection(self) -> Optional[Sequence[str]]:
        return self._solid_selection

    @property
    def mode(self) -> Optional[str]:
        return self._mode

    @property
    def partitions_def(self) -> PartitionsDefinition:
        return self._partitions_def

    def run_config_for_partition(self, partition: Partition[T]) -> Mapping[str, Any]:
        return copy.deepcopy(self._user_defined_run_config_fn_for_partition(partition))

    def tags_for_partition(self, partition: Partition[T]) -> Mapping[str, str]:
        user_tags = validate_tags(
            self._user_defined_tags_fn_for_partition(partition), allow_reserved_tags=False
        )
        tags = merge_dicts(user_tags, DagsterRun.tags_for_partition_set(self, partition))

        return tags

    def get_partitions(
        self,
        current_time: Optional[datetime] = None,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> Sequence[Partition[T]]:
        """Return the set of known partitions.

        Arguments:
            current_time (Optional[datetime]): The evaluation time for the partition function, which
                is passed through to the ``partition_fn`` (if it accepts a parameter).  Defaults to
                the current time in UTC.

        """
        return self._partitions_def.get_partitions(
            current_time, dynamic_partitions_store=dynamic_partitions_store
        )

    def get_partition(
        self, name: str, dynamic_partitions_store: Optional[DynamicPartitionsStore] = None
    ) -> Partition[T]:
        for partition in self.get_partitions(dynamic_partitions_store=dynamic_partitions_store):
            if partition.name == name:
                return partition

        raise DagsterUnknownPartitionError(f"Could not find a partition with key `{name}`")

    def get_partition_names(
        self,
        current_time: Optional[datetime] = None,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> Sequence[str]:
        return [part.name for part in self.get_partitions(current_time, dynamic_partitions_store)]

    def create_schedule_definition(
        self,
        schedule_name: str,
        cron_schedule: str,
        partition_selector: PartitionSelectorFunction,
        should_execute: Optional[Callable[..., bool]] = None,
        environment_vars: Optional[Mapping[str, str]] = None,
        execution_timezone: Optional[str] = None,
        description: Optional[str] = None,
        decorated_fn: Optional[PartitionScheduleFunction] = None,
        job: Optional[ExecutableDefinition] = None,
        default_status=DefaultScheduleStatus.STOPPED,
    ) -> "PartitionScheduleDefinition":
        """Create a ScheduleDefinition from a PartitionSetDefinition.

        Arguments:
            schedule_name (str): The name of the schedule.
            cron_schedule (str): A valid cron string for the schedule
            partition_selector (Callable[[ScheduleEvaluationContext, PartitionSetDefinition], Union[Partition, Sequence[Partition]]]):
                Function that determines the partition to use at a given execution time. Can return
                either a single Partition or a list of Partitions. For time-based partition sets,
                will likely be either `identity_partition_selector` or a selector returned by
                `create_offset_partition_selector`.
            should_execute (Optional[function]): Function that runs at schedule execution time that
                determines whether a schedule should execute. Defaults to a function that always returns
                ``True``.
            environment_vars (Optional[dict]): The environment variables to set for the schedule.
            execution_timezone (Optional[str]): Timezone in which the schedule should run.
                Supported strings for timezones are the ones provided by the
                `IANA time zone database <https://www.iana.org/time-zones>` - e.g. "America/Los_Angeles".
            description (Optional[str]): A human-readable description of the schedule.
            default_status (DefaultScheduleStatus): Whether the schedule starts as running or not. The default
                status can be overridden from Dagit or via the GraphQL API.

        Returns:
            PartitionScheduleDefinition: The generated PartitionScheduleDefinition for the partition
                selector
        """
        check.str_param(schedule_name, "schedule_name")
        check.str_param(cron_schedule, "cron_schedule")
        check.opt_callable_param(should_execute, "should_execute")
        check.opt_mapping_param(environment_vars, "environment_vars", key_type=str, value_type=str)
        check.callable_param(partition_selector, "partition_selector")
        check.opt_str_param(execution_timezone, "execution_timezone")
        check.opt_str_param(description, "description")
        check.inst_param(default_status, "default_status", DefaultScheduleStatus)

        def _execution_fn(context):
            check.inst_param(context, "context", ScheduleEvaluationContext)
            with user_code_error_boundary(
                ScheduleExecutionError,
                lambda: f"Error occurred during the execution of partition_selector for schedule {schedule_name}",
            ):
                selector_result = partition_selector(context, self)

            if isinstance(selector_result, SkipReason):
                yield selector_result
                return

            selected_partitions = (
                selector_result
                if isinstance(selector_result, (frozenlist, list))
                else [selector_result]
            )

            check.is_list(selected_partitions, of_type=Partition)

            if not selected_partitions:
                yield SkipReason("Partition selector returned an empty list of partitions.")
                return

            partition_names = self.get_partition_names(context.scheduled_execution_time)

            missing_partition_names = [
                partition.name
                for partition in selected_partitions
                if partition.name not in partition_names
            ]

            if missing_partition_names:
                yield SkipReason(
                    "Partition selector returned partition"
                    + ("s" if len(missing_partition_names) > 1 else "")
                    + f" not in the partition set: {', '.join(missing_partition_names)}."
                )
                return

            with user_code_error_boundary(
                ScheduleExecutionError,
                lambda: f"Error occurred during the execution of should_execute for schedule {schedule_name}",
            ):
                if should_execute and not should_execute(context):
                    yield SkipReason(
                        "should_execute function for {schedule_name} returned false.".format(
                            schedule_name=schedule_name
                        )
                    )
                    return

            for selected_partition in selected_partitions:
                with user_code_error_boundary(
                    ScheduleExecutionError,
                    lambda: f"Error occurred during the execution of run_config_fn for schedule {schedule_name}",
                ):
                    run_config = self.run_config_for_partition(selected_partition)

                with user_code_error_boundary(
                    ScheduleExecutionError,
                    lambda: f"Error occurred during the execution of tags_fn for schedule {schedule_name}",
                ):
                    tags = self.tags_for_partition(selected_partition)
                yield RunRequest(
                    run_key=selected_partition.name if len(selected_partitions) > 0 else None,
                    run_config=run_config,
                    tags=tags,
                )

        return PartitionScheduleDefinition(
            name=schedule_name,
            cron_schedule=cron_schedule,
            pipeline_name=self._pipeline_name,
            tags_fn=None,
            should_execute=None,
            environment_vars=environment_vars,
            partition_set=self,
            execution_timezone=execution_timezone,
            execution_fn=_execution_fn,
            description=description,
            decorated_fn=decorated_fn,
            job=job,
            default_status=default_status,
        )


class PartitionScheduleDefinition(ScheduleDefinition):
    __slots__ = ["_partition_set"]

    def __init__(
        self,
        name: str,
        cron_schedule: str,
        pipeline_name: Optional[str],
        tags_fn: Optional[ScheduleTagsFunction],
        should_execute: Optional[ScheduleShouldExecuteFunction],
        partition_set: PartitionSetDefinition,
        environment_vars: Optional[Mapping[str, str]] = None,
        run_config_fn: Optional[ScheduleRunConfigFunction] = None,
        execution_timezone: Optional[str] = None,
        execution_fn: Optional[ScheduleExecutionFunction] = None,
        description: Optional[str] = None,
        decorated_fn: Optional[PartitionScheduleFunction] = None,
        job: Optional[ExecutableDefinition] = None,
        default_status: DefaultScheduleStatus = DefaultScheduleStatus.STOPPED,
    ):
        super(PartitionScheduleDefinition, self).__init__(
            name=check_valid_name(name),
            cron_schedule=cron_schedule,
            job_name=pipeline_name,
            run_config_fn=run_config_fn,
            tags_fn=tags_fn,
            should_execute=should_execute,
            environment_vars=environment_vars,
            execution_timezone=execution_timezone,
            execution_fn=execution_fn,
            description=description,
            job=job,
            default_status=default_status,
        )
        self._partition_set = check.inst_param(
            partition_set, "partition_set", PartitionSetDefinition
        )
        self._decorated_fn = check.opt_callable_param(decorated_fn, "decorated_fn")

    def __call__(self, *args, **kwargs) -> Mapping[str, Any]:
        if not self._decorated_fn:
            raise DagsterInvalidInvocationError(
                "Only partition schedules created using one of the partition schedule decorators "
                "can be directly invoked."
            )
        if len(args) == 0 and len(kwargs) == 0:
            raise DagsterInvalidInvocationError(
                "Schedule decorated function has date argument, but no date argument was "
                "provided when invoking."
            )
        if len(args) + len(kwargs) > 1:
            raise DagsterInvalidInvocationError(
                "Schedule invocation received multiple arguments. Only a first "
                "positional date parameter should be provided when invoking."
            )

        date_param_name = get_function_params(self._decorated_fn)[0].name

        if args:
            date = check.opt_inst_param(args[0], date_param_name, datetime)
        else:
            if date_param_name not in kwargs:
                raise DagsterInvalidInvocationError(
                    f"Schedule invocation expected argument '{date_param_name}'."
                )
            date = check.opt_inst_param(kwargs[date_param_name], date_param_name, datetime)

        return self._decorated_fn(date)  # type: ignore

    def get_partition_set(self) -> PartitionSetDefinition:
        return self._partition_set


class PartitionedConfig(Generic[T]):
    """Defines a way of configuring a job where the job can be run on one of a discrete set of
    partitions, and each partition corresponds to run configuration for the job.

    Setting PartitionedConfig as the config for a job allows you to launch backfills for that job
    and view the run history across partitions.
    """

    def __init__(
        self,
        partitions_def: PartitionsDefinition[T],  # pylint: disable=unsubscriptable-object
        run_config_for_partition_fn: Callable[[Partition[T]], Mapping[str, Any]],
        decorated_fn: Optional[Callable[..., Mapping[str, Any]]] = None,
        tags_for_partition_fn: Optional[Callable[[Partition[T]], Mapping[str, str]]] = None,
    ):
        self._partitions = check.inst_param(partitions_def, "partitions_def", PartitionsDefinition)
        self._run_config_for_partition_fn = check.callable_param(
            run_config_for_partition_fn, "run_config_for_partition_fn"
        )
        self._decorated_fn = decorated_fn
        self._tags_for_partition_fn = check.opt_callable_param(
            tags_for_partition_fn, "tags_for_partition_fn"
        )

    @public
    @property
    def partitions_def(self) -> PartitionsDefinition[T]:  # pylint: disable=unsubscriptable-object
        return self._partitions

    @public
    @property
    def run_config_for_partition_fn(self) -> Callable[[Partition[T]], Mapping[str, Any]]:
        return self._run_config_for_partition_fn

    @public
    @property
    def tags_for_partition_fn(self) -> Optional[Callable[[Partition[T]], Mapping[str, str]]]:
        return self._tags_for_partition_fn

    def get_partition_keys(self, current_time: Optional[datetime] = None) -> Sequence[str]:
        return [partition.name for partition in self.partitions_def.get_partitions(current_time)]

    def get_run_config_for_partition_key(self, partition_key: str) -> Mapping[str, Any]:
        """Generates the run config corresponding to a partition key.

        Args:
            partition_key (str): the key for a partition that should be used to generate a run config.
        """
        partitions = self.partitions_def.get_partitions()
        partition = [p for p in partitions if p.name == partition_key]
        if len(partition) == 0:
            raise DagsterInvalidInvocationError(f"No partition for partition key {partition_key}.")
        return self.run_config_for_partition_fn(partition[0])

    @classmethod
    def from_flexible_config(
        cls,
        config: Optional[Union[ConfigMapping, Mapping[str, object], "PartitionedConfig"]],
        partitions_def: PartitionsDefinition,
    ) -> "PartitionedConfig":
        check.invariant(
            not isinstance(config, ConfigMapping),
            "Can't supply a ConfigMapping for 'config' when 'partitions_def' is supplied.",
        )

        if isinstance(config, PartitionedConfig):
            check.invariant(
                config.partitions_def == partitions_def,
                (
                    "Can't supply a PartitionedConfig for 'config' with a different "
                    "PartitionsDefinition than supplied for 'partitions_def'."
                ),
            )
            return config
        else:
            hardcoded_config = config if config else {}
            return cls(partitions_def, lambda _: cast(Mapping, hardcoded_config))

    def __call__(self, *args, **kwargs):
        if self._decorated_fn is None:
            raise DagsterInvalidInvocationError(
                "Only PartitionedConfig objects created using one of the partitioned config "
                "decorators can be directly invoked."
            )
        else:
            return self._decorated_fn(*args, **kwargs)


def static_partitioned_config(
    partition_keys: Sequence[str],
    tags_for_partition_fn: Optional[Callable[[str], Mapping[str, str]]] = None,
) -> Callable[[Callable[[str], Mapping[str, Any]]], PartitionedConfig]:
    """Creates a static partitioned config for a job.

    The provided partition_keys is a static list of strings identifying the set of partitions. The
    list of partitions is static, so while the run config returned by the decorated function may
    change over time, the list of valid partition keys does not.

    This has performance advantages over `dynamic_partitioned_config` in terms of loading different
    partition views in Dagit.

    The decorated function takes in a partition key and returns a valid run config for a particular
    target job.

    Args:
        partition_keys (Sequence[str]): A list of valid partition keys, which serve as the range of
            values that can be provided to the decorated run config function.
        tags_for_partition_fn (Optional[Callable[[str], Mapping[str, str]]]): A function that
            accepts a partition key and returns a dictionary of tags to attach to runs for that
            partition.

    Returns:
        PartitionedConfig
    """
    check.sequence_param(partition_keys, "partition_keys", str)

    def inner(fn: Callable[[str], Mapping[str, Any]]) -> PartitionedConfig:
        check.callable_param(fn, "fn")

        def _run_config_wrapper(partition: Partition) -> Mapping[str, Any]:
            return fn(partition.name)

        def _tag_wrapper(partition: Partition) -> Mapping[str, str]:
            return tags_for_partition_fn(partition.name) if tags_for_partition_fn else {}

        return PartitionedConfig(
            partitions_def=StaticPartitionsDefinition(partition_keys),
            run_config_for_partition_fn=_run_config_wrapper,
            decorated_fn=fn,
            tags_for_partition_fn=_tag_wrapper,
        )

    return inner


def dynamic_partitioned_config(
    partition_fn: Callable[[Optional[datetime]], Sequence[str]],
    tags_for_partition_fn: Optional[Callable[[str], Mapping[str, str]]] = None,
) -> Callable[[Callable[[str], Mapping[str, Any]]], PartitionedConfig]:
    """Creates a dynamic partitioned config for a job.

    The provided partition_fn returns a list of strings identifying the set of partitions, given
    an optional datetime argument (representing the current time).  The list of partitions returned
    may change over time.

    The decorated function takes in a partition key and returns a valid run config for a particular
    target job.

    Args:
        partition_fn (Callable[[datetime.datetime], Sequence[str]]): A function that generates a
            list of valid partition keys, which serve as the range of values that can be provided
            to the decorated run config function.
        tags_for_partition_fn (Optional[Callable[[str], Mapping[str, str]]]): A function that
            accepts a partition key and returns a dictionary of tags to attach to runs for that
            partition.

    Returns:
        PartitionedConfig
    """
    check.callable_param(partition_fn, "partition_fn")

    def inner(fn: Callable[[str], Mapping[str, Any]]) -> PartitionedConfig:
        def _run_config_wrapper(partition: Partition) -> Mapping[str, Any]:
            return fn(partition.name)

        def _tag_wrapper(partition: Partition) -> Mapping[str, str]:
            return tags_for_partition_fn(partition.name) if tags_for_partition_fn else {}

        return PartitionedConfig(
            partitions_def=DynamicPartitionsDefinition(partition_fn),
            run_config_for_partition_fn=_run_config_wrapper,
            decorated_fn=fn,
            tags_for_partition_fn=_tag_wrapper,
        )

    return inner


def cron_schedule_from_schedule_type_and_offsets(
    schedule_type: ScheduleType,
    minute_offset: int,
    hour_offset: int,
    day_offset: Optional[int],
):
    if schedule_type is ScheduleType.HOURLY:
        return f"{minute_offset} * * * *"
    elif schedule_type is ScheduleType.DAILY:
        return f"{minute_offset} {hour_offset} * * *"
    elif schedule_type is ScheduleType.WEEKLY:
        return f"{minute_offset} {hour_offset} * * {day_offset if day_offset is not None else 0}"
    elif schedule_type is ScheduleType.MONTHLY:
        return f"{minute_offset} {hour_offset} {day_offset if day_offset is not None else 1} * *"
    else:
        check.assert_never(schedule_type)


class PartitionsSubset(ABC):
    """Represents a subset of the partitions within a PartitionsDefinition."""

    @abstractmethod
    def get_partition_keys_not_in_subset(
        self,
        current_time: Optional[datetime] = None,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> Iterable[str]:
        raise NotImplementedError()

    @abstractmethod
    def get_partition_keys(self, current_time: Optional[datetime] = None) -> Iterable[str]:
        raise NotImplementedError()

    @abstractmethod
    def get_partition_key_ranges(
        self,
        current_time: Optional[datetime] = None,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> Sequence[PartitionKeyRange]:
        raise NotImplementedError()

    @abstractmethod
    def with_partition_keys(self, partition_keys: Iterable[str]) -> "PartitionsSubset":
        raise NotImplementedError()

    def with_partition_key_range(
        self,
        partition_key_range: PartitionKeyRange,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> "PartitionsSubset":
        return self.with_partition_keys(
            self.partitions_def.get_partition_keys_in_range(
                partition_key_range, dynamic_partitions_store=dynamic_partitions_store
            )
        )

    def __or__(self, other: "PartitionsSubset") -> "PartitionsSubset":
        if self is other:
            return self
        return self.with_partition_keys(other.get_partition_keys())

    @abstractmethod
    def serialize(self) -> str:
        raise NotImplementedError()

    @classmethod
    @abstractmethod
    def from_serialized(
        cls, partitions_def: PartitionsDefinition, serialized: str
    ) -> "PartitionsSubset":
        raise NotImplementedError()

    @classmethod
    @abstractmethod
    def can_deserialize(
        cls,
        partitions_def: PartitionsDefinition,
        serialized: str,
        serialized_partitions_def_unique_id: Optional[str],
        serialized_partitions_def_class_name: Optional[str],
    ) -> bool:
        raise NotImplementedError()

    @property
    @abstractmethod
    def partitions_def(self) -> PartitionsDefinition:
        raise NotImplementedError()

    @abstractmethod
    def __len__(self) -> int:
        raise NotImplementedError()

    @abstractmethod
    def __contains__(self, value) -> bool:
        raise NotImplementedError()

    @classmethod
    @abstractmethod
    def empty_subset(cls, partitions_def: PartitionsDefinition) -> "PartitionsSubset":
        raise NotImplementedError()


class DefaultPartitionsSubset(PartitionsSubset):
    # Every time we change the serialization format, we should increment the version number.
    # This will ensure that we can gracefully degrade when deserializing old data.
    SERIALIZATION_VERSION = 1

    def __init__(self, partitions_def: PartitionsDefinition, subset: Optional[Set[str]] = None):
        check.opt_set_param(subset, "subset")
        self._partitions_def = partitions_def
        self._subset = subset or set()

    def get_partition_keys_not_in_subset(
        self,
        current_time: Optional[datetime] = None,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> Iterable[str]:
        return (
            set(
                self._partitions_def.get_partition_keys(
                    current_time=current_time, dynamic_partitions_store=dynamic_partitions_store
                )
            )
            - self._subset
        )

    def get_partition_keys(self, current_time: Optional[datetime] = None) -> Iterable[str]:
        return self._subset

    def get_partition_key_ranges(
        self,
        current_time: Optional[datetime] = None,
        dynamic_partitions_store: Optional[DynamicPartitionsStore] = None,
    ) -> Sequence[PartitionKeyRange]:
        partition_keys = self._partitions_def.get_partition_keys(
            current_time, dynamic_partitions_store=dynamic_partitions_store
        )
        cur_range_start = None
        cur_range_end = None
        result = []
        for partition_key in partition_keys:
            if partition_key in self._subset:
                if cur_range_start is None:
                    cur_range_start = partition_key
                cur_range_end = partition_key
            else:
                if cur_range_start is not None and cur_range_end is not None:
                    result.append(PartitionKeyRange(cur_range_start, cur_range_end))
                cur_range_start = cur_range_end = None

        if cur_range_start is not None and cur_range_end is not None:
            result.append(PartitionKeyRange(cur_range_start, cur_range_end))

        return result

    def with_partition_keys(self, partition_keys: Iterable[str]) -> "DefaultPartitionsSubset":
        return DefaultPartitionsSubset(
            self._partitions_def,
            self._subset | set(partition_keys),
        )

    def serialize(self) -> str:
        # Serialize version number, so attempting to deserialize old versions can be handled gracefully.
        # Any time the serialization format changes, we should increment the version number.
        return json.dumps({"version": self.SERIALIZATION_VERSION, "subset": list(self._subset)})

    @classmethod
    def from_serialized(
        cls, partitions_def: PartitionsDefinition, serialized: str
    ) -> "PartitionsSubset":
        # Check the version number, so only valid versions can be deserialized.
        data = json.loads(serialized)

        if isinstance(data, list):
            # backwards compatibility
            return cls(subset=set(data), partitions_def=partitions_def)
        else:
            if data.get("version") != cls.SERIALIZATION_VERSION:
                raise DagsterInvalidDeserializationVersionError(
                    f"Attempted to deserialize partition subset with version {data.get('version')},"
                    f" but only version {cls.SERIALIZATION_VERSION} is supported."
                )
            return cls(subset=set(data.get("subset")), partitions_def=partitions_def)

    @classmethod
    def can_deserialize(
        cls,
        partitions_def: PartitionsDefinition,
        serialized: str,
        serialized_partitions_def_unique_id: Optional[str],
        serialized_partitions_def_class_name: Optional[str],
    ) -> bool:
        if serialized_partitions_def_class_name is not None:
            return serialized_partitions_def_class_name == partitions_def.__class__.__name__

        data = json.loads(serialized)
        return isinstance(data, list) or (
            data.get("subset") is not None and data.get("version") == cls.SERIALIZATION_VERSION
        )

    @property
    def partitions_def(self) -> PartitionsDefinition:
        return self._partitions_def

    def __eq__(self, other):
        return (
            isinstance(other, DefaultPartitionsSubset)
            and self._partitions_def == other._partitions_def
            and self._subset == other._subset
        )

    def __len__(self) -> int:
        return len(self._subset)

    def __contains__(self, value) -> bool:
        return value in self._subset

    def __repr__(self) -> str:
        return (
            f"DefaultPartitionsSubset(subset={self._subset}, partitions_def={self._partitions_def})"
        )

    @classmethod
    def empty_subset(cls, partitions_def: PartitionsDefinition) -> "PartitionsSubset":
        return cls(partitions_def=partitions_def)
