import sys
import itertools
from typing import Any, Dict, ByteString, Iterable, Iterator, Optional, Sequence, Type, Union as TypeUnion

from .base import Serializer, SerializerMeta, SerializerMetadata
from ._utils import copy_if_mutable, type_name, as_type, get_as_value, iter_chunks, instanceoverride

__all__ = ['Array']


class ArrayMetadata(SerializerMetadata):
    def __init__(self, *, array_size: slice, serializer: Serializer):
        super().__init__(array_size.start * serializer._heracles_bytesize_())
        self.array_size = array_size
        self.serializer = serializer


class ArrayMeta(SerializerMeta):
    def __getitem__(cls, args) -> Type['Array']:
        # Array type syntax only works with `Array[size, type]`
        if not issubclass(cls, Array) or not isinstance(args, tuple) or len(args) != 2:
            return super().__getitem__(args)

        size, serializer = args
        if isinstance(size, int):
            if size < 0:
                raise ValueError(f'Array size must not be negative, got {size}')
            size = slice(size, size)
        elif isinstance(size, slice):
            if size.step is not None:
                raise ValueError('Cannot supply step to Array size')
            elif size.start is None:  # Open start is treated as 0
                size = slice(0, size.stop)

            if not isinstance(size.start, int) or not isinstance(size.stop, (int, type(None))):
                raise ValueError('Array min and max size must be integers')
            elif size.start < 0:
                raise ValueError(f'Array min size must not be negative, got {size.start}')
            elif size.stop is not None and size.stop < size.start:
                raise ValueError('Array max size must be greater than its min size')
        else:
            raise TypeError(f'Expected int or a slice as array size, got {type_name(size)}')

        if not issubclass(as_type(serializer), Serializer):
            raise TypeError(f'Array element type must be a Serializer')
        # Variable-sized arrays rely on assigned values to function, so hidden serializers are
        # invalid in that context because they cannot be assigned
        elif serializer._heracles_hidden_() and size.start != size.stop:
            raise ValueError(f'Array of {type_name(serializer)} cannot be variable size')

        return type(type_name(cls), (cls,), {
            SerializerMeta.METAATTR: ArrayMetadata(
                array_size=size, serializer=get_as_value(serializer))
        })

    def repr_array_size(cls) -> str:
        size = cls._heracles_metadata_().array_size
        if cls._heracles_vst_():
            max_size = '' if size.stop is None else size.stop
            return f'{size.start}:{max_size}'
        else:
            return f'{size.start}'

    def __repr__(cls) -> str:
        return f'<array <{cls._heracles_metadata_().serializer}> [{cls.repr_array_size()}]>'


class Array(Serializer, metaclass=ArrayMeta):
    def __init__(self, value: Sequence = (), *args, **kwargs):
        return super().__init__(value, *args, **kwargs)
    
    @classmethod
    def _heracles_hidden_(cls) -> bool:
        return cls._heracles_metadata_().serializer._heracles_hidden_()

    @classmethod
    def _heracles_vst_(cls) -> bool:
        size = cls._heracles_metadata_().array_size
        return size.start != size.stop

    @instanceoverride
    def _heracles_bytesize_(self, value: Optional[TypeUnion['Array', Sequence]] = None) -> int:
        value = self._heracles_validate_(value)
        meta = self._heracles_metadata_()
        return max(meta.array_size.start, len(value)) * meta.serializer._heracles_bytesize_()

    def _heracles_validate_(self, value: Optional[TypeUnion['Array', Sequence]] = None) -> Sequence:
        value = self._get_serializer_value(value)
        size = self._heracles_metadata_().array_size
        if size.stop is not None and len(value) > size.stop:
            raise ValueError('Assigned value size exceeds array size')

        serializer = self._heracles_metadata_().serializer
        for i in value:
            serializer._heracles_validate_(i)
        return value

    def serialize_value(self, value: TypeUnion['Array', Sequence], settings: Dict[str, Any] = None) -> bytes:
        value = self._heracles_validate_(value)
        serialized = bytearray()
        serializer = self._heracles_metadata_().serializer
        for i, v in enumerate(value):
            try:
                serialized.extend(serializer.serialize_value(v, settings))
            except ValueError as e:
                raise ValueError(f'Invalid data in index `{i}` of array: {e.message}')
        remaining_elements = (self._heracles_bytesize_(value) - len(serialized)) // serializer._heracles_bytesize_()
        for _ in range(remaining_elements):
            serialized.extend(serializer.serialize())
        return bytes(serialized)

    def _to_array_repr(self, value: Iterable) -> TypeUnion[list, tuple, str, bytes]:
        # Try to guess the best representation of the array for the user by inspecting
        # the type of the initial value, if exists, or of the underlying serializer
        # otherwise, falling back to a default representation if we couldn't guess.
        check_value = self.value if self.value else self._heracles_metadata_().serializer.value
        if type(value) == type(check_value):
            return value
        elif isinstance(check_value, bytes):
            return b''.join(bytes((v,)) if isinstance(v, int) else v for v in value)
        elif isinstance(check_value, str):
            # This is a C-string, so strip NUL chars from the end
            return ''.join(value).rstrip('\x00')
        else:
            return type(self.value)(value)

    def deserialize(self, raw_data: ByteString, settings: Dict[str, Any] = None) -> Sequence:
        size = self._heracles_metadata_().array_size
        serializer = self._heracles_metadata_().serializer
        min_size = size.start * serializer._heracles_bytesize_()
        if min_size > len(raw_data):
            raise ValueError(f'Raw data ({len(raw_data)}) is too short (expected {min_size})')
        if size.stop is not None:
            max_size = size.stop * serializer._heracles_bytesize_()
            if len(raw_data) > max_size:
                raise ValueError(f'Raw data ({len(raw_data)}) is too long (expected {max_size})')
        if len(raw_data) % serializer._heracles_bytesize_() != 0:
            raise ValueError("Raw data size isn't divisible by array element size")
        values_iterator = (
            serializer.deserialize(chunk, settings)
            for chunk in iter_chunks(raw_data, serializer._heracles_bytesize_()))
        return self._to_array_repr(values_iterator)

    def _heracles_render_(self, value: Optional[TypeUnion['Array', Sequence]] = None) -> str:
        value = self._heracles_validate_(value)
        serializer = self._heracles_metadata_().serializer
        return f'{serializer}[{type(self).repr_array_size()}] {repr(self._to_array_repr(value))}'
    
    def _heracles_compare_(self, other: TypeUnion['Array', Sequence], value: Optional[TypeUnion['Array', Sequence]] = None) -> bool:
        if other is None:
            return False
        value = self._get_serializer_value(value)
        size = self._heracles_metadata_().array_size
        if size.stop is not None and max(len(other), len(value)) > size.stop:
            return False
        return all(a == b for a, b in itertools.zip_longest(
            value, other, fillvalue=self._heracles_metadata_().serializer._get_serializer_value()))

    def __len__(self) -> int:
        return max(self._heracles_metadata_().array_size.start, len(self.value))

    def __getitem__(self, idx) -> Any:
        if idx < 0:
            idx = len(self) - idx
        size = self._heracles_metadata_().array_size
        serializer = self._heracles_metadata_().serializer
        if idx < 0 or size.stop is not None and idx >= size.stop:
            raise IndexError('Array index out of range')
        try:
            return serializer._get_serializer_value(self.value[idx])
        except IndexError:
            return copy_if_mutable(serializer.value)

    def __iter__(self) -> Iterator:
        return (self[i] for i in range(len(self)))
