"""This module contains functions for patching property objects generated by pyodata library"""

from odfuzz.generators import RandomGenerator
from odfuzz.constants import BOOLEAN_OPERATORS, EXPRESSION_OPERATORS


def patch_proprties(entity_set):
    for proprty in entity_set.entity_type.proprties():
        patch_proprty_max_length(proprty)
        patch_proprty_generator(proprty)
        patch_proprty_operator(proprty)


def patch_proprty_max_length(proprty):
    proprty_type = proprty.typ.name
    if proprty_type == 'Edm.String':
        proprty.max_string_length = max_string_length(proprty.max_length)


def max_string_length(max_length):
    if not max_length:
        return 100
    return max_length


def patch_proprty_generator(proprty):
    proprty_type = proprty.typ.name
    if proprty_type == 'Edm.String':
        proprty.generate = RandomGenerator.edm_string.__get__(proprty, None)
    elif proprty_type == 'Edm.DateTime':
        proprty.generate = RandomGenerator.edm_datetime
    elif proprty_type == 'Edm.Boolean':
        proprty.generate = RandomGenerator.edm_boolean
    elif proprty_type == 'Edm.Byte':
        proprty.generate = RandomGenerator.edm_byte
    elif proprty_type == 'Edm.SByte':
        proprty.generate = RandomGenerator.edm_sbyte
    elif proprty_type == 'Edm.Single':
        proprty.generate = RandomGenerator.edm_single
    elif proprty_type == 'Edm.Guid':
        proprty.generate = RandomGenerator.edm_guid
    elif proprty_type == 'Edm.Decimal':
        proprty.generate = RandomGenerator.edm_decimal.__get__(proprty, None)
    elif proprty_type == 'Edm.DateTimeOffset':
        proprty.generate = RandomGenerator.edm_datetimeoffset
    elif proprty_type == 'Edm.Time':
        proprty.generate = RandomGenerator.edm_time
    elif proprty_type == 'Edm.Binary':
        proprty.generate = RandomGenerator.edm_binary
    elif proprty_type.startswith('Edm.Int'):
        if proprty_type.endswith('16'):
            proprty.generate = RandomGenerator.edm_int16
        elif proprty_type.endswith('32'):
            proprty.generate = RandomGenerator.edm_int32
        elif proprty_type.endswith('64'):
            proprty.generate = RandomGenerator.edm_int64
        else:
            proprty.generate = lambda: None
    else:
        proprty.generate = lambda: None


def patch_proprty_operator(proprty):
    proprty_type = proprty.typ.name
    if proprty_type == 'Edm.Boolean':
        proprty.operators = BOOLEAN_OPERATORS
    else:
        proprty.operators = EXPRESSION_OPERATORS