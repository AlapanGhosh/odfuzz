"""This module contains a builder class and wrapper classes for queryable entities."""

import copy
import random
import inspect
import uuid

from abc import ABCMeta, abstractmethod
from collections import namedtuple

from pyodata.v2.model import Edmx
from pyodata.exceptions import PyODataException

from odfuzz.exceptions import BuilderError, DispatcherError
from odfuzz.generators import RandomGenerator
from odfuzz.monkey import patch_proprties
from odfuzz.config import Config
from odfuzz.constants import *

NullEntity = namedtuple('NullEntity', 'name')
StringSelf = namedtuple('StringSelf', 'max_string_length')
OptionRestriction = namedtuple('OptionRestriction', 'restr is_not_restricted')


class Builder(object):
    """A class for building and initializing all queryable entities."""

    def __init__(self, dispatcher, restrictions):
        self._restrictions = restrictions
        self._dispatcher = dispatcher
        self._queryable = QueryableEntities()

    def build(self):
        data_model = self._get_data_model()
        for entity_set in data_model.entity_sets:
            patch_proprties(entity_set)
            principal_entities = get_principal_entities(data_model, entity_set)
            query_group = QueryGroup(entity_set, self._restrictions, self._dispatcher, principal_entities)
            if query_group.query_options():
                self._queryable.add(query_group)
        return self._queryable

    def _get_data_model(self):
        metadata_response = self._get_metadata_from_service()
        try:
            service_model = Edmx.parse(metadata_response.content)
        except PyODataException as pyodata_ex:
            raise BuilderError('An exception occurred while parsing metadata: {}'.format(pyodata_ex))
        return service_model

    def _get_metadata_from_service(self):
        metadata_request = '$metadata?' + 'sap-client=' + Config.client
        try:
            metadata_response = self._dispatcher.get(metadata_request, timeout=5)
        except DispatcherError as disp_error:
            raise BuilderError('An exception occurred while retrieving metadata: {}'.format(disp_error))
        if metadata_response.status_code != 200:
            raise BuilderError('Cannot retrieve metadata from {}. Status code is {}'.format(
                self._dispatcher.service, metadata_response.status_code))
        return metadata_response


class QueryableEntities(object):
    """A wrapper that holds a reference to all queryable entities."""

    def __init__(self):
        self._entities = {}

    def add(self, query_group):
        self._entities[query_group.entity_set.name] = query_group

    def get_entity(self, entity_name):
        return self._entities[entity_name]

    def all(self):
        return self._entities.values()


class QueryGroup(object):
    """A group of query options applicable to one entity set."""

    def __init__(self, entity_set, restrictions, dispatcher, principal_entities):
        self._entity_set = entity_set
        self._restrictions = restrictions
        self._dispatcher = dispatcher
        self._principal_entities = principal_entities
        self._query_options = {}
        self._accessible_entity = None

        self._query_options_list = []
        self._required_options = []
        self._init_group()
        self._init_accessible_entity_path()

    @property
    def entity_set(self):
        return self._entity_set

    @property
    def principal_entities(self):
        return self._principal_entities

    def principal_entity(self, entity_set_name):
        found_entity = None
        for entity in self._principal_entities:
            if entity.name == entity_set_name:
                found_entity = entity
        return found_entity

    def get_accessible_entity_set(self):
        return self._accessible_entity.get_queryable_entity()

    def query_options(self):
        return self._query_options.values()

    def query_option(self, option_name):
        return self._query_options[option_name]

    def random_options(self):
        list_length = len(self._query_options_list)
        if list_length == 0:
            sample_length = 0
        else:
            sample_length = round(random.random() * (list_length - 1)) + 1
        sample_options = random.sample(self._query_options_list, sample_length)
        selected_options = sample_options + self._required_options
        random.shuffle(selected_options)
        return selected_options

    def _init_group(self):
        self._init_filter_query()
        self._init_orderby_query()
        self._init_expand_query()
        self._init_query_type(TOP, 'topable', TopQuery, self._dispatcher)
        self._init_query_type(SKIP, 'pageable', SkipQuery, self._dispatcher)

    def _init_query_type(self, option_name, metadata_attr, query_object, dispatcher):
        option_restr = self._get_restrictions(option_name)
        is_queryable = getattr(self._entity_set, metadata_attr)

        if is_queryable and option_restr.is_not_restricted:
            self._query_options[option_name] = query_object(self._entity_set, option_restr.restr, dispatcher)
            include_restrictions = getattr(option_restr.restr, 'include', None)
            if include_restrictions and include_restrictions.get(self._entity_set.name):
                self._required_options.append(self._query_options[option_name])
            else:
                self._query_options_list.append(self._query_options[option_name])

    def _init_filter_query(self):
        option_restr = self._get_restrictions(FILTER)
        if option_restr.restr:
            exclude_restrictions = option_restr.restr.exclude
            draft_restrictions = self._get_draft_restrictions()
            draft_properties = get_draft_properties(self._entity_set.name, draft_restrictions)
            needs_filter = True
        else:
            exclude_restrictions = []
            draft_properties = []
            needs_filter = self._entity_set.requires_filter
        entity_set = self._delete_restricted_proprties(exclude_restrictions, 'filterable', draft_properties)
        self._entity_set._req_filter = entity_set._req_filter = needs_filter

        if option_restr.is_not_restricted and entity_set.entity_type.proprties() or draft_properties:
            self._query_options[FILTER] = FilterQuery(entity_set, option_restr.restr, draft_properties)
            self._add_filter_option_to_list(entity_set)

    def _get_draft_restrictions(self):
        try:
            draft_restrictions = self._restrictions.restriction(DRAFT_OBJECTS)
        except KeyError:
            draft_restrictions = None
        return draft_restrictions

    def _init_orderby_query(self):
        option_restr = self._get_restrictions(ORDERBY)
        if option_restr.restr:
            exclude_restrictions = option_restr.restr.exclude
        else:
            exclude_restrictions = []
        entity_set = self._delete_restricted_proprties(exclude_restrictions, 'sortable', [])

        if option_restr.is_not_restricted and entity_set.entity_type.proprties():
            self._query_options[ORDERBY] = OrderbyQuery(entity_set, option_restr.restr)
            self._query_options_list.append(self._query_options[ORDERBY])

    def _init_expand_query(self):
        option_restr = self._get_restrictions(EXPAND)
        if option_restr.restr:
            entity_set = self._delete_restricted_nav_proprties(option_restr.restr)
        else:
            entity_set = self._entity_set
        if option_restr.is_not_restricted and self._entity_set.entity_type.nav_proprties:
            self._query_options[EXPAND] = ExpandQuery(entity_set, option_restr.restr)
            self._query_options_list.append(self._query_options[EXPAND])

    def _delete_restricted_nav_proprties(self, option_restrictions):
        entity_set = self._entity_set
        if option_restrictions.exclude:
            entity_set = copy.deepcopy(self._entity_set)
            restricted_proprties = set(option_restrictions.exclude.get(NAV_PROPRTY, []))
            restricted_proprties |= set(option_restrictions.exclude.get(self._entity_set.name, []))
            for navigation_proprty in self._entity_set.entity_type.nav_proprties:
                if navigation_proprty.name in restricted_proprties:
                    del entity_set.entity_type._nav_properties[navigation_proprty.name]
        return entity_set

    def _get_restrictions(self, option_name):
        if self._restrictions:
            query_restr = self._restrictions.restriction(option_name)
            is_not_restricted = self._is_not_restricted(query_restr.exclude)
        else:
            query_restr = None
            is_not_restricted = True
        return OptionRestriction(query_restr, is_not_restricted)

    def _add_filter_option_to_list(self, entity_set):
        if entity_set.requires_filter:
            self._required_options.append(self._query_options[FILTER])
        else:
            self._query_options_list.append(self._query_options[FILTER])

    def _is_not_restricted(self, exclude_restr):
        if not exclude_restr:
            return True
        restricted_entities = exclude_restr.get(GLOBAL_ENTITY)
        if restricted_entities:
            if self._entity_set.name in restricted_entities:
                return False
        return True

    def _delete_restricted_proprties(self, exclude_restr, attribute, draft_proprties):
        entity_set = copy.deepcopy(self._entity_set)
        if exclude_restr:
            restr_proprty_list = exclude_restr.get(self._entity_set.name, [])
            restr_proprty_list.extend(exclude_restr.get(GLOBAL_PROPRTY, []))
        else:
            restr_proprty_list = []

        for proprty in self._entity_set.entity_type.proprties():
            if (proprty.name in restr_proprty_list or not getattr(proprty, attribute)) \
                    and proprty.name not in draft_proprties:
                del entity_set.entity_type._properties[proprty.name]

        return entity_set

    def _init_accessible_entity_path(self):
        if self._entity_set.addressable:
            self._accessible_entity = AddressableEntity(self._entity_set, self._principal_entities)
        else:
            self._accessible_entity = NonAddressableEntity(self._entity_set, self._principal_entities)


class QueryOption(metaclass=ABCMeta):
    """An abstract class for a query option."""

    def __init__(self, entity_set, name, dollar, restrictions=None):
        self._entity_set = entity_set
        self._name = name
        self._restrictions = restrictions
        self._dollar = dollar

    @property
    def entity_set(self):
        return self._entity_set

    @property
    def name(self):
        return self._name

    @property
    def restrictions(self):
        return self._restrictions

    @property
    def dollar(self):
        return self._dollar

    @abstractmethod
    def apply_restrictions(self):
        pass

    @abstractmethod
    def generate(self, depending_data):
        pass

    @abstractmethod
    def get_depending_data(self):
        pass


class ExpandQuery(QueryOption):
    def __init__(self, entity, restrictions):
        super(ExpandQuery, self).__init__(entity, EXPAND, '$', restrictions)
        self._navigation_paths = set()
        self._init_possible_paths()

    def _init_possible_paths(self):
        for navigation_proprty in self._entity_set.entity_type.nav_proprties:
            possible_paths = [navigation_proprty.name]
            # TODO: search deeper for navigation properties
            for inner_nav_proprty in navigation_proprty.to_role.entity_type.nav_proprties:
                possible_paths.append(navigation_proprty.name + '/' + inner_nav_proprty.name)
            self._navigation_paths.update(possible_paths)

    def apply_restrictions(self):
        pass

    def generate(self, depending_data):
        option = ExpandOption()
        entities_to_expand = self._get_random_entity_paths()
        option.option_string = ','.join(entities_to_expand)
        option.add_entity_paths(entities_to_expand)
        return option

    def _get_random_entity_paths(self):
        paths_num = len(self._navigation_paths)
        if paths_num > MAX_EXPAND_VALUES:
            max_values = MAX_EXPAND_VALUES
        else:
            max_values = paths_num
        num_of_entities = round(random.random() * (max_values - 1)) + 1
        random_entities = random.sample(self._navigation_paths, num_of_entities)
        return random_entities

    def get_depending_data(self):
        return None


class OrderbyQuery(QueryOption):
    """The search query option."""

    def __init__(self, entity, restrictions):
        super(OrderbyQuery, self).__init__(entity, ORDERBY, '$', restrictions)
        self._proprties = set(proprty.name for proprty in self.entity_set.entity_type.proprties())

    def apply_restrictions(self):
        pass

    def generate(self, depending_data):
        option = OrderbyOption([], '')
        total_proprties = len(self._proprties)
        if total_proprties > 3:
            total_proprties = 3
        sample_size = round(random.random() * (total_proprties - 1)) + 1

        for proprty in random.sample(self._proprties, sample_size):
            option.add_proprty(proprty)
        option.order = random.choice(['asc', 'desc'])
        option.option_string = OrderbyOptionBuilder(option).build()
        return option

    def get_depending_data(self):
        return None


class TopQuery(QueryOption):
    """The $top query option."""

    def __init__(self, entity, restrictions, dispatcher):
        super(TopQuery, self).__init__(entity, TOP, '$', restrictions)
        self._dispatcher = dispatcher
        self._max_range_prob = {INT_MAX: 1.0}
        self._depending_data = 0
        self.apply_restrictions()

    def apply_restrictions(self):
        self._set_max_range()

    def generate(self, depending_data):
        option = TopOption()
        selected_value = weighted_random(self._max_range_prob.items())
        skip_value = depending_data.get(SKIP)
        if skip_value and skip_value + selected_value > INT_MAX:
            max_range = INT_MAX - skip_value
        else:
            max_range = selected_value
        top_value = round(random.random() * max_range)
        option.option_string = str(top_value)
        self._depending_data = top_value
        return option

    def get_depending_data(self):
        return self._depending_data

    def _set_max_range(self):
        self._max_range_prob.update({INT_MAX: 0.001})
        include_restr = getattr(self.restrictions, 'include', None)
        if include_restr:
            max_values = include_restr.get(self.entity_set.name)
            if max_values:
                total_entities = int(max_values[0])
            else:
                total_entities = self._get_total_entities()
        else:
            total_entities = self._get_total_entities()

        if total_entities > 1000:
            self._max_range_prob[1000] = 0.999
        else:
            self._max_range_prob[total_entities] = 0.999

    def _get_total_entities(self):
        try:
            url = self._entity_set.name + '/' + '$count?' + 'sap-client=' + Config.client
            response = self._dispatcher.get(url, timeout=5)
        except DispatcherError:
            total_entities = INT_MAX
        else:
            try:
                total_entities = int(response.text)
            except ValueError:
                total_entities = INT_MAX
        return total_entities


class SkipQuery(QueryOption):
    """The $skip query option."""

    def __init__(self, entity, restrictions, dispatcher):
        super(SkipQuery, self).__init__(entity, SKIP, '$', restrictions)
        self._dispatcher = dispatcher
        self._max_range_prob = {INT_MAX: 1.0}
        self._depending_data = 0
        self.apply_restrictions()

    def apply_restrictions(self):
        self._set_max_range()

    def generate(self, depending_data):
        option = SkipOption()
        selected_value = weighted_random(self._max_range_prob.items())
        top_value = depending_data.get(TOP)
        if top_value and top_value + selected_value > INT_MAX:
            max_range = INT_MAX - top_value
        else:
            max_range = selected_value
        skip_value = round(random.random() * max_range)
        option.option_string = str(skip_value)
        self._depending_data = skip_value
        return option

    def get_depending_data(self):
        return self._depending_data

    def _set_max_range(self):
        self._max_range_prob.update({INT_MAX: 0.001})
        include_restr = getattr(self.restrictions, 'include', None)
        if include_restr:
            max_values = include_restr.get(self.entity_set.name)
            if max_values:
                total_entities = int(max_values[0])
            else:
                total_entities = INT_MAX
        else:
            total_entities = INT_MAX

        if total_entities == INT_MAX:
            self._max_range_prob[total_entities] = 1
        else:
            self._max_range_prob[total_entities] = 0.999


class FilterQuery(QueryOption):
    """The $filter query option."""

    def __init__(self, entity, restrictions, draft_properties):
        super(FilterQuery, self).__init__(entity, FILTER, '$', restrictions)

        self._functions = FilterFunctionsGroup(entity.entity_type.proprties(), restrictions)
        if not self._functions.group:
            self._noterm_function = self._generate_proprty
        else:
            self._noterm_function = self._generate_function

        self._recursion_depth = 0
        self._finalizing_groups = 0
        self._right_part = False
        self._option = None
        self._groups_stack = None
        self._option_string = ''

        self._filterable_proprties = list(self._entity_set.entity_type.proprties())
        if draft_properties:
            self._draft_proprty = self.entity_set.entity_type.proprty(draft_properties[0])
        else:
            self._draft_proprty = None

        self._proprties = None
        self._required_proprties = []
        self._init_required_proprties()

        self._set_generators_for_restricted_proprties()

    def _init_required_proprties(self):
        for proprty in self._filterable_proprties:
            if proprty.required_in_filter or self._draft_proprty and proprty.name == self._draft_proprty.name:
                self._append_required_proprty(proprty, False)

    def _append_required_proprty(self, proprty, is_reaplaceable):
        setattr(proprty, 'replaceable', is_reaplaceable)
        self._required_proprties.append(proprty)

    def apply_restrictions(self):
        return None

    def generate(self, depending_data):
        self._init_variables()
        self._generate_string()

        self._option.reverse_logicals()
        self._option.delete_redundancies()
        self._option.option_string = self._option_string
        return self._option

    def get_depending_data(self):
        pass

    def _init_variables(self):
        self._recursion_depth = 0
        self._finalizing_groups = 0
        self._right_part = False
        self._option = FilterOption([], [], [])
        self._groups_stack = Stack()
        self._option_string = ''
        self._proprties = ProprtiesSelector(self._filterable_proprties.copy(), self._required_proprties.copy())

    def _set_generators_for_restricted_proprties(self):
        for proprty in self._entity_set.entity_type.proprties():
            if proprty.filter_restriction == 'interval':
                proprty.generate_remaining_proprties = self._generate_interval_values
            elif proprty.filter_restriction == 'multi-value':
                proprty.generate_remaining_proprties = self._generate_multi_values
            else:
                proprty.generate_remaining_proprties = lambda x, y: None

    def _generate_string(self):
        if not self._required_proprties and random.random() < SINGLE_VALUE_PROB:
            self._option.add_part()
            self._generate_proprty()
        else:
            self._noterm_expression()

    def _generate_interval_values(self, proprty, used_operator):
        if used_operator != 'eq':
            operator = 'ge' if used_operator == 'le' else 'le'
            self._generate_next_interval_value(proprty, operator)

    def _generate_next_interval_value(self, proprty, operator):
        logical = 'and'
        self._generate_sap_restricted(proprty, logical, operator)
        self._set_right_logical_references()

    def _generate_multi_values(self, proprty, operator):
        logical = 'or'
        total_values = round(random.random() * (MAX_MULTI_VALUES - 1) + 1)
        for _ in range(total_values):
            self._generate_sap_restricted(proprty, logical, operator)
            self._set_right_logical_references()

    def _generate_sap_restricted(self, proprty, logical, operator):
        self._option_string += ' ' + logical + ' '
        self._option.add_logical()

        last_logical = self._option.last_logical
        last_logical['name'] = logical
        self._update_left_logical_references(last_logical)

        self._option.add_part()
        operand = proprty.generate()
        self._option_string += proprty.name + ' ' + operator + ' ' + operand
        replaceable = getattr(proprty, 'replaceable', True)
        self._update_proprty_part(proprty.name, operator, operand, replaceable)

    def _noterm_expression(self):
        self._recursion_depth += 1
        if (not self._proprties.has_remaining()) \
                and (random.random() < 0.5 or self._recursion_depth > RECURSION_LIMIT):
            self._generate_element()
        else:
            self._noterm_child()

    def _noterm_parent(self):
        if (not self._proprties.has_remaining()) \
                and (random.random() < 0.5 or self._recursion_depth > RECURSION_LIMIT):
            self._noterm_expression()
        else:
            self._generate_child()

    def _generate_child(self):
        if random.random() < 0.5 or not self._proprties.has_remaining():
            self._noterm_child()
        else:
            self._generate_child_group()

    def _generate_child_group(self):
        self._option_string += '('
        self._option.add_group()
        last_group = self._option.last_group
        if self._right_part:
            self._update_group_references(last_group)
        self._groups_stack.push(last_group)
        self._noterm_child()
        self._finalizing_groups += 1
        self._option_string += ')'

    def _update_group_references(self, last_group):
        last_logical = self._option.last_logical
        last_logical['right_id'] = last_group['id']
        last_group['left_id'] = last_logical['id']
        stacked_group = self._groups_stack.top()
        if stacked_group:
            stacked_group['logicals'].append(last_logical['id'])

    def _noterm_child(self):
        self._noterm_parent()
        self._generate_rest()

    def _generate_rest(self):
        if self._proprties.has_filterable():
            self._noterm_logical()
            self._noterm_parent()

    def _noterm_logical(self):
        operator = weighted_random(LOGICAL_OPERATORS.items())
        self._option_string += ' ' + operator + ' '

        self._option.add_logical()
        last_logical = self._option.last_logical
        last_logical['name'] = operator

        if self._finalizing_groups:
            popped_group = self._groups_stack.pop(self._finalizing_groups)
            self._finalizing_groups = 0
            last_logical['left_id'] = popped_group['id']
            popped_group['right_id'] = last_logical['id']
        else:
            self._update_left_logical_references(last_logical)
        self._right_part = True

    def _update_left_logical_references(self, last_logical):
        stacked_group = self._groups_stack.top()
        if stacked_group:
            last_logical['group_id'] = stacked_group['id']
        last_logical['left_id'] = self._option.last_part['id']
        self._option.last_part['right_id'] = last_logical['id']

    def _generate_element(self):
        self._option.add_part()
        if random.random() < FUNCTION_WEIGHT:
            self._noterm_function()
        else:
            self._generate_proprty()

    def _generate_function(self):
        functions_wrapper = random.choice(list(self._functions.group.values()))
        functions_dict = get_methods_dict(functions_wrapper.__class__)
        function_call = random.choice(list(functions_dict.values()))

        generated_function = function_call(functions_wrapper)
        operator = weighted_random(generated_function.operators.items())
        operand = generated_function.generate()
        self._option_string += generated_function.generated_string + ' ' + operator + ' ' + operand
        self._update_function_part(generated_function, operator, operand)
        self._update_right_logical_references()

    def _update_function_part(self, generated_function, operator, operand):
        last_part = self._option.last_part
        last_part['name'] = generated_function.generated_string
        last_part['operator'] = operator
        last_part['operand'] = operand
        last_part['proprties'] = generated_function.proprties
        last_part['params'] = generated_function.params
        last_part['func'] = generated_function.function_type.name
        last_part['return_type'] = generated_function.function_type.return_type

    def _generate_proprty(self):
        proprty = self._proprties.get_random_proprty()
        operator = weighted_random(proprty.operators.get_all())
        operand = proprty.generate()
        self._option_string += proprty.name + ' ' + operator + ' ' + operand
        replaceable = getattr(proprty, 'replaceable', True)
        self._update_proprty_part(proprty.name, operator, operand, replaceable)

        self._update_right_logical_references()
        proprty.generate_remaining_proprties(proprty, operator)

    def _get_random_index_to_proprties(self):
        random_index = round(random.random() * (len(self._proprties) - 1))
        return random_index

    def _update_proprty_part(self, proprty_name, operator, operand, replaceable):
        last_part = self._option.last_part
        last_part['name'] = proprty_name
        last_part['operator'] = operator
        last_part['operand'] = operand
        last_part['replaceable'] = replaceable

    def _update_right_logical_references(self):
        if self._right_part:
            self._right_part = False
            self._set_right_logical_references()

    def _set_right_logical_references(self):
        last_logical = self._option.last_logical
        last_part = self._option.last_part

        last_logical['right_id'] = last_part['id']
        last_part['left_id'] = last_logical['id']

        last_group = self._groups_stack.top()
        if last_group:
            last_group['logicals'].append(last_logical['id'])
            last_logical['group_id'] = last_group['id']


class ProprtiesSelector(object):
    def __init__(self, filterable_proprties, required_proprties):
        self._filterable_proprties = filterable_proprties
        self._required_proprties = required_proprties

        self._required_tuple = None
        self._non_required_tuple = None
        self._has_remaining_proprties = True if self._required_proprties else False
        self._init_tuples()

        self._proprties = [self._non_required_tuple, self._required_tuple]
        self._times_called_has_remaining = 0

    def has_remaining(self):
        if self._times_called_has_remaining >= len(self._required_proprties):
            return False
        else:
            self._times_called_has_remaining += 1
            return self._has_remaining_proprties

    def has_filterable(self):
        return self._filterable_proprties

    def get_random_proprty(self):
        proprties_group = weighted_random(self._proprties)
        proprty = proprties_group.get_proprty()
        self._update_remaining_proprties()
        return proprty

    def _init_tuples(self):
        non_required_proprties = list(set(self._filterable_proprties) - set(self._required_proprties))
        if non_required_proprties:
            if self._required_proprties:
                self._non_required_tuple = (NonRequiredProprties(self._filterable_proprties), 0.01)
                self._required_tuple = (RequiredProprties(self._required_proprties), 0.99)
            else:
                self._non_required_tuple = (NonRequiredProprties(self._filterable_proprties), 1.0)
                self._required_tuple = (RequiredProprties([]), 0.0)
        else:
            self._non_required_tuple = (NonRequiredProprties([]), 0.0)
            self._required_tuple = (RequiredProprties(self._required_proprties), 1.0)

    def _update_remaining_proprties(self):
        if self._has_remaining_proprties:
            required_tuple = self._proprties[1]
            required_group = required_tuple[0]
            if not required_group.proprties:
                self._has_remaining_proprties = False
                self._proprties[0] = (NonRequiredProprties(self._filterable_proprties), 1.0)
                del self._proprties[1]


class ProprtiesGroup(object):
    def __init__(self, proprties):
        self._proprties = proprties

    @property
    def proprties(self):
        return self._proprties

    def _delete_filterable_if_restricted(self, filter_restriction, index):
        if filter_restriction == 'single-value':
            del self._proprties[index]


class RequiredProprties(ProprtiesGroup):
    def get_proprty(self):
        random_index = round(random.random() * (len(self._proprties) - 1))
        proprty = self._proprties.pop(random_index)
        return proprty


class NonRequiredProprties(ProprtiesGroup):
    def get_proprty(self):
        random_index = round(random.random() * (len(self._proprties) - 1))
        proprty = self._proprties[random_index]
        self._delete_filterable_if_restricted(proprty.filter_restriction, random_index)
        return proprty


class Option(metaclass=ABCMeta):
    """An option container."""

    def __init__(self):
        self._option_string = ''

    @property
    def option_string(self):
        return self._option_string

    @option_string.setter
    def option_string(self, value):
        self._option_string = value

    @property
    @abstractmethod
    def data(self):
        pass


class ExpandOption(Option):
    def __init__(self):
        super(ExpandOption, self).__init__()
        self._entity_paths = set()

    @property
    def data(self):
        return list(self._entity_paths)

    def add_entity_paths(self, entity_paths):
        self._entity_paths.update(entity_paths)


class SkipOption(Option):
    """A skip option container holding an integer value."""

    @property
    def data(self):
        return self._option_string


class TopOption(Option):
    """A top option container holding an integer value."""

    @property
    def data(self):
        return self._option_string


class OrderbyOption(Option):
    """An orderby option container holding a list of used properties and type of order operation."""

    def __init__(self, proprties, order):
        super(OrderbyOption, self).__init__()
        self._proprties = proprties
        self._order = order

    @property
    def data(self):
        data_dict = {'proprties': self._proprties, 'order': self._order}
        return data_dict

    @property
    def order(self):
        return self._order

    @order.setter
    def order(self, value):
        self._order = value

    def add_proprty(self, proprty_name):
        self._proprties.append(proprty_name)


class FilterOption(Option):
    """A filter option container holding cross-references and data of logical parts and groups."""

    def __init__(self, logicals, parts, groups):
        super(FilterOption, self).__init__()
        self._logicals = logicals
        self._parts = parts
        self._groups = groups
        self._option_string = ''

    @property
    def logicals(self):
        return self._logicals

    @property
    def parts(self):
        return self._parts

    @property
    def groups(self):
        return self._groups

    @property
    def last_part(self):
        return self._parts[-1]

    @property
    def last_logical(self):
        return self._logicals[-1]

    @property
    def last_group(self):
        return self._groups[-1]

    @property
    def data(self):
        data_dict = {'groups': self._groups, 'logicals': self._logicals,
                     'parts': self._parts}
        return data_dict

    @last_part.setter
    def last_part(self, value):
        self._parts[-1] = value

    @last_logical.setter
    def last_logical(self, value):
        self._logicals[-1] = value

    def add_logical(self):
        logical_id = str(uuid.UUID(int=random.getrandbits(128), version=4))
        self._logicals.append({'id': logical_id})

    def add_part(self):
        part_id = str(uuid.UUID(int=random.getrandbits(128), version=4))
        self._parts.append({'id': part_id})

    def add_group(self):
        group_id = str(uuid.UUID(int=random.getrandbits(128), version=4))
        self._groups.append({'id': group_id, 'logicals': []})

    def logical_by_id(self, id_logical):
        for logical in self._logicals:
            if logical['id'] == id_logical:
                return logical
        return None

    def part_by_id(self, id_part):
        for part in self._parts:
            if part['id'] == id_part:
                return part
        return None

    def group_by_id(self, id_group):
        for group in self._groups:
            if group['id'] == id_group:
                return group
        return None

    def reverse_logicals(self):
        self._logicals = list(reversed(self._logicals))

    def delete_redundancies(self):
        filtered_groups = []
        redundant_groups_id = []

        for group in self._groups:
            if group['logicals'] and (group.get('right_id', None) or group.get('left_id', None)):
                filtered_groups.append(group)
            else:
                redundant_groups_id.append(group['id'])
        self._groups[:] = filtered_groups

        for logical in self._logicals:
            if logical.get('group_id', None) in redundant_groups_id:
                logical.pop('group_id')


class Stack(object):
    """An abstract stack data type."""

    def __init__(self):
        self._stack = []

    def push(self, element):
        self._stack.append(element)

    def top(self):
        if self._stack:
            return self._stack[-1]
        return None

    def pop(self, elements_to_pop=1):
        popped_element = None
        for _ in range(elements_to_pop):
            popped_element = self._pop_one()
        return popped_element

    def _pop_one(self):
        if self._stack:
            return self._stack.pop()
        return None


class OrderbyOptionBuilder(object):
    """An orderby option string builder."""

    def __init__(self, option):
        self._option = option
        self._option_string = None

    def build(self):
        if not self._option_string:
            self._option_string = ''
            option_data = self._option.data
            for proprty in option_data['proprties']:
                self._option_string += proprty + ','
            self._option_string = self._option_string.rstrip(',')
            self._option_string += ' ' + option_data['order']
        return self._option_string


class FilterOptionBuilder(object):
    """A filter option string builder."""

    def __init__(self, option):
        self._option = option
        self._option_string = None
        self._used_logicals = []

    def build(self):
        if not self._option_string:
            self._option_string = ''
            if len(self._option.parts) == 1:
                self._option_string = build_filter_part(self._option.last_part)
            else:
                self._build_all(self._option.logicals[0])
        return self._option_string

    def _build_all(self, first_logical):
        if 'group_id' in first_logical:
            self._build_first_group(first_logical['group_id'])
        else:
            self._used_logicals.append(first_logical['id'])
            self._option_string = self._build_left(first_logical) + ' ' + first_logical['name'] \
                                                                  + ' ' + self._build_right(first_logical)
        self._check_last_logical()

    def _build_first_group(self, group_id):
        group = self._option.group_by_id(group_id)
        self._option_string = self._build_group(group)
        if 'left_id' in group:
            self._option_string = self._build_surroundings(True, group, self._option_string)
        if 'right_id' in group:
            self._option_string = self._build_surroundings(False, group, self._option_string)

    def _build_left(self, part):
        left_id = part['left_id']
        option_string = self._build_by_id(left_id, True)
        return option_string

    def _build_right(self, part):
        right_id = part['right_id']
        option_string = self._build_by_id(right_id, False)
        return option_string

    def _build_by_id(self, part_id, skip_left):
        part = self._option.part_by_id(part_id)
        if part:
            generated_string = build_filter_part(part)
            generated_string = self._build_surroundings(skip_left, part, generated_string)
        else:
            group = self._option.group_by_id(part_id)
            generated_string = self._build_group(group)
            generated_string = self._build_surroundings(skip_left, group, generated_string)
        return generated_string

    def _build_surroundings(self, skip_left, part, generated_string):
        if skip_left and 'left_id' in part:
            left_logical = self._option.logical_by_id(part['left_id'])
            self._used_logicals.append(left_logical['id'])
            generated_string = self._build_left(left_logical) + ' ' + left_logical['name']\
                                                              + ' ' + generated_string
        if not skip_left and 'right_id' in part:
            right_logical = self._option.logical_by_id(part['right_id'])
            self._used_logicals.append(right_logical['id'])
            generated_string += ' ' + right_logical['name'] + ' ' + self._build_right(right_logical)
        return generated_string

    def _build_group(self, group):
        first_logical_id = group['logicals'][0]
        logical = self._option.logical_by_id(first_logical_id)
        self._used_logicals.append(logical['id'])
        group_string = '(' + self._build_left(logical) + ' ' + logical['name']\
                           + ' ' + self._build_right(logical) + ')'
        return group_string

    def _check_last_logical(self):
        if self._option.last_logical['id'] not in self._used_logicals:
            logical = self._option.last_logical
            self._option_string = self._build_left(logical) + ' ' + logical['name']\
                                                            + ' ' + '(' + self._option_string + ')'


class FilterOptionDeleter(object):
    """A filter option remover that deletes a random part next to the selected logical operator."""

    def __init__(self, option_value, logical):
        self._option_value = option_value
        self._logical = logical
        self._selected_id = None
        self._remained_id = None
        self._deleting_part = None
        self._remaining_part = None

    def remove_adjacent(self, selected_id):
        self._remove_logical_in_group()
        self._init_selection(selected_id)
        if self._deleting_part:
            self._option_value['parts'].remove(self._deleting_part)
            self._add_part_references()
        else:
            self._deleting_part = dict_by_id(self._option_value['groups'],
                                             self._logical[self._selected_id])
            self._option_value['groups'].remove(self._deleting_part)
            self._add_part_references()
            self._remove_all(self._deleting_part)
        for group in self._option_value['groups'][:]:
            if not group['logicals']:
                self._option_value['groups'].remove(group)

    def _remove_logical_in_group(self):
        group_id = self._logical.get('group_id')
        if group_id:
            remove_logical_from_group(self._option_value, group_id, self._logical['id'])

    def _init_selection(self, selected_id):
        self._selected_id = selected_id
        self._remained_id = 'left_id' if self._selected_id.startswith('right') else 'right_id'
        self._deleting_part = dict_by_id(self._option_value['parts'],
                                         self._logical[self._selected_id])
        self._remaining_part = get_part_by_id(self._option_value, self._logical,
                                              self._remained_id)

    def _add_part_references(self):
        if self._selected_id in self._deleting_part:
            self._manage_part_references()
        else:
            self._remaining_part.pop(self._selected_id)
        if 'group_id' in self._logical:
            self._manage_group_references()

    def _manage_part_references(self):
        self._remaining_part[self._selected_id] = self._deleting_part[self._selected_id]
        if self._option_value['logicals']:
            referencing_logical = dict_by_id(self._option_value['logicals'],
                                             self._deleting_part[self._selected_id])
            referencing_logical[self._remained_id] = self._remaining_part['id']

    def _manage_group_references(self):
        group_border = dict_by_id(self._option_value['groups'], self._logical['group_id'])
        if not group_border['logicals']:
            try:
                self._option_value['groups'].remove(group_border)
            except ValueError:
                pass
            else:
                self._update_group_references(group_border, self._option_value['logicals'])

    def _update_group_references(self, group_border, logicals):
        if self._selected_id in group_border:
            remaining_logical = dict_by_id(logicals, group_border[self._selected_id])
            self._remaining_part[self._selected_id] = group_border[self._selected_id]
            remaining_logical[self._remained_id] = self._remaining_part['id']
        if self._remained_id in group_border:
            remaining_logical = dict_by_id(logicals, group_border[self._remained_id])
            self._remaining_part[self._remained_id] = group_border[self._remained_id]
            remaining_logical[self._selected_id] = self._remaining_part['id']

    def _remove_all(self, part):
        for logical in self._option_value['logicals'][:]:
            if 'group_id' in logical:
                if logical['group_id'] == part['id']:
                    self._option_value['logicals'].remove(logical)
                    remove_by_reference(self._option_value['parts'], logical['left_id'])
                    remove_by_reference(self._option_value['parts'], logical['right_id'])
                    part_del = dict_by_id(self._option_value['groups'], logical['left_id'])
                    if part_del:
                        self._option_value['groups'].remove(part_del)
                        self._remove_all(part_del)
                    part_del = dict_by_id(self._option_value['groups'], logical['right_id'])
                    if part_del:
                        self._option_value['groups'].remove(part_del)
                        self._remove_all(part_del)


class FilterFunctionsGroup(object):
    """A wrapper for a group of all functions supported by the filter query option."""

    def __init__(self, filterable_proprties, restrictions):
        self._group = {}
        self._init_functions_group(filterable_proprties)

        if self._group and restrictions:
            self._apply_restrictions(restrictions.exclude)

    @property
    def group(self):
        return self._group

    def _init_functions_group(self, filterable_proprties):
        for proprty in filterable_proprties:
            if proprty.filter_restriction:
                continue
            if proprty.typ.name == 'Edm.String':
                self._group.setdefault('String', StringFilterFunctions(FunctionsGenerator())).add_proprty(proprty)
            elif proprty.typ.name == 'Edm.DateTime':
                self._group.setdefault('Date', DateFilterFunctions(FunctionsGenerator())).add_proprty(proprty)
            elif proprty.typ.name == 'Edm.Decimal':
                self._group.setdefault('Math', MathFilterFunctions(FunctionsGenerator())).add_proprty(proprty)

    def _apply_restrictions(self, exclude_restrictions):
        if exclude_restrictions:
            restricted_functions = exclude_restrictions.get(GLOBAL_FUNCTION, None)
            if restricted_functions:
                self._delete_restricted_functions(restricted_functions)

    def _delete_restricted_functions(self, restricted_functions):
        for key, functions_wrapper in list(self._group.items()):
            methods_dict = get_methods_dict(functions_wrapper)
            for restricted_function in restricted_functions:
                method_name = 'func_' + restricted_function
                if method_name in methods_dict:
                    delattr(functions_wrapper.__class__, method_name)
            if not get_methods_dict(functions_wrapper.__class__).values():
                self._group.pop(key)


class FunctionsGenerator:
    def __init__(self):
        self._random = random

    def __getattr__(self, item):
        def func_wrap(*args, **kwargs):
            return getattr(self._random, item)(*args, **kwargs)
        return func_wrap

    def edm_string(self, proprty):
        return RandomGenerator.edm_string(proprty)


class FilterFunctions:
    def __init__(self, random_generator):
        self._random_generator = random_generator
        self._probability = DATE_FUNC_PROB
        self._proprties = []

    @property
    def proprties(self):
        return self._proprties

    @property
    def probability(self):
        return self._probability

    @probability.setter
    def probability(self, probability_number):
        self._probability = probability_number

    def add_proprty(self, proprty_object):
        self._proprties.append(proprty_object)


class DateFilterFunctions(FilterFunctions):
    """A wrapper of corresponding date filter functions family."""

    def func_day(self):
        proprty = self._random_generator.choice(self._proprties)
        generated_string = 'day({})'.format(proprty.name)
        return FilterFunction([proprty.name], None, generated_string, FunctionsInt('day'))

    def func_hour(self):
        proprty = self._random_generator.choice(self._proprties)
        generated_string = 'hour({})'.format(proprty.name)
        return FilterFunction([proprty.name], None, generated_string, FunctionsInt('hour'))

    def func_minute(self):
        proprty = self._random_generator.choice(self._proprties)
        generated_string = 'minute({})'.format(proprty.name)
        return FilterFunction([proprty.name], None, generated_string, FunctionsInt('minute'))

    def func_month(self):
        proprty = self._random_generator.choice(self._proprties)
        generated_string = 'month({})'.format(proprty.name)
        return FilterFunction([proprty.name], None, generated_string, FunctionsInt('month'))

    def func_second(self):
        proprty = self._random_generator.choice(self._proprties)
        generated_string = 'second({})'.format(proprty.name)
        return FilterFunction([proprty.name], None, generated_string, FunctionsInt('second'))

    def func_year(self):
        proprty = self._random_generator.choice(self._proprties)
        generated_string = 'year({})'.format(proprty.name)
        return FilterFunction([proprty.name], None, generated_string, FunctionsInt('second'))


class MathFilterFunctions(FilterFunctions):
    """A wrapper of corresponding math filter functions family."""

    def func_round(self):
        proprty = self._random_generator.choice(self._proprties)
        generated_string = 'round({})'.format(proprty.name)
        return FilterFunction([proprty.name], None, generated_string, FunctionsInt('round'))

    def func_floor(self):
        proprty = self._random_generator.choice(self._proprties)
        generated_string = 'floor({})'.format(proprty.name)
        return FilterFunction([proprty.name], None, generated_string, FunctionsInt('floor'))

    def func_ceiling(self):
        proprty = self._random_generator.choice(self._proprties)
        generated_string = 'ceiling({})'.format(proprty.name)
        return FilterFunction([proprty.name], None, generated_string, FunctionsInt('ceiling'))


class StringFilterFunctions(FilterFunctions):
    """A wrapper of corresponding string filter functions family."""

    SUBSTRING_PARAM_NUM = 1

    def func_substringof(self):
        proprty = self._random_generator.choice(self._proprties)
        value = proprty.generate()
        generated_string = 'substringof({}, {})'.format(proprty.name, value)
        return FilterFunction([proprty.name], [value], generated_string, FunctionsBool('substringof'))

    def func_endswith(self):
        proprty = self._random_generator.choice(self._proprties)
        value = proprty.generate()
        generated_string = 'endswith({}, {})'.format(proprty.name, value)
        return FilterFunction([proprty.name], [value], generated_string, FunctionsBool('endswith'))

    def func_startswith(self):
        proprty = self._random_generator.choice(self._proprties)
        value = proprty.generate()
        generated_string = 'startswith({}, {})'.format(proprty.name, value)
        return FilterFunction([proprty.name], [value], generated_string, FunctionsBool('startswith'))

    def func_length(self):
        proprty = self._random_generator.choice(self._proprties)
        value = proprty.generate()
        generated_string = 'length({})'.format(proprty.name)
        return FilterFunction([proprty.name], [value], generated_string, FunctionsInt('length'))

    def func_indexof(self):
        proprty = self._random_generator.choice(self._proprties)
        value = proprty.generate()
        generated_string = 'indexof({}, {})'.format(proprty.name, value)
        return FilterFunction([proprty.name], [value], generated_string, FunctionsInt('indexof'))

    def func_replace(self):
        proprty = self._random_generator.choice(self._proprties)
        literal1 = self._random_generator.edm_string(proprty)
        literal2 = self._random_generator.edm_string(proprty)
        generated_string = 'replace({}, {}, {})'.format(proprty.name, literal1, literal2)
        return FilterFunction([proprty.name], [literal1, literal2], generated_string,
                              FunctionsString('replace', proprty))

    def func_substring(self):
        proprty = self._random_generator.choice(self._proprties)
        generated_string = 'substring(' + proprty.name
        params = []
        for _ in range(StringFilterFunctions.SUBSTRING_PARAM_NUM):
            int32 = str(self._random_generator.randint(-2147483648, 2147483647))
            generated_string += ', ' + int32
            params.append(int32)
        generated_string += ')'
        return FilterFunction([proprty.name], params, generated_string, FunctionsString('substring', proprty))

    def func_tolower(self):
        proprty = self._random_generator.choice(self._proprties)
        generated_string = 'tolower({})'.format(proprty.name)
        return FilterFunction([proprty.name], None, generated_string, FunctionsString('tolower', proprty))

    def func_toupper(self):
        proprty = self._random_generator.choice(self._proprties)
        generated_string = 'toupper({})'.format(proprty.name)
        return FilterFunction([proprty.name], None, generated_string, FunctionsString('toupper', proprty))

    def func_trim(self):
        proprty = self._random_generator.choice(self._proprties)
        generated_string = 'trim({})'.format(proprty.name)
        return FilterFunction([proprty.name], None, generated_string, FunctionsString('trim', proprty))

    def func_concat(self):
        proprty = self._random_generator.choice(self._proprties)
        if self._random_generator.random() > 0.5:
            value = self._random_generator.edm_string(proprty)
            proprty_list = [proprty.name]
            param_list = [value]
            generated_string = 'concat({}, {})'.format(proprty.name, value)
        else:
            proprty2 = self._random_generator.choice(self._proprties)
            proprty_list = [proprty.name, proprty2.name]
            param_list = None
            generated_string = 'concat({}, {})'.format(proprty.name, proprty2.name)

        max_length = 0
        for proprty in proprty_list:
            max_length += proprty.max_string_length
        return FilterFunction(proprty_list, param_list, generated_string,
                              FunctionsString('concat', StringSelf(max_length)))


class FunctionsReturnType(object):
    """A type of filter query option function."""

    def __init__(self, return_type, operators, name, generator):
        self._return_type = return_type
        self._operators = operators
        self._name = name
        self._generator = generator

    @property
    def return_type(self):
        return self._return_type

    @property
    def operators(self):
        return self._operators

    @property
    def name(self):
        return self._name

    def generate(self):
        return self._generator()


class FunctionsInt(FunctionsReturnType):
    def __init__(self, name):
        super(FunctionsInt, self).__init__('Edm.Int32', EXPRESSION_OPERATORS, name, RandomGenerator.edm_int32)


class FunctionsString(FunctionsReturnType):
    def __init__(self, name, proprty):
        self._proprty = proprty
        super(FunctionsString, self).__init__('Edm.String', EXPRESSION_OPERATORS, name, RandomGenerator.edm_string)

    def generate(self):
        return self._generator(self._proprty)


class FunctionsBool(FunctionsReturnType):
    def __init__(self, name):
        super(FunctionsBool, self).__init__('Edm.Boolean', BOOLEAN_OPERATORS, name, RandomGenerator.edm_boolean)


class FilterFunction(object):
    """A container of generated filter function."""

    def __init__(self, proprties, params, generated_string, function_type):
        self._proprties = proprties
        self._params = params
        self._generated_string = generated_string
        self._function_type = function_type

    @property
    def proprties(self):
        return self._proprties

    @property
    def params(self):
        return self._params

    @property
    def generated_string(self):
        return self._generated_string

    @property
    def operators(self):
        return self._function_type.operators

    @property
    def function_type(self):
        return self._function_type

    def generate(self):
        return self._function_type.generate()


class EntitySet(metaclass=ABCMeta):
    def __init__(self, entity_set, principal_entities):
        self._entity_set = entity_set
        self._principal_entities = principal_entities

    @abstractmethod
    def get_queryable_entity(self):
        pass


class AddressableEntity(EntitySet):
    def get_queryable_entity(self):
        accessible_entity = AccessibleEntity(self._entity_set, {}, NullEntity(''))
        return accessible_entity


class NonAddressableEntity(EntitySet):
    def __init__(self, entity_set, principal_entities):
        super(NonAddressableEntity, self).__init__(entity_set, principal_entities)
        patch_proprties(entity_set)

    def get_queryable_entity(self):
        if random.random() > EMPTY_ENTITY_PROB:
            if self._principal_entities and random.random() < ASSOCIATED_ENTITY_PROB:
                containing_entity_set = random.choice(self._principal_entities)
                principal_entity_set = containing_entity_set
            else:
                containing_entity_set = self._entity_set
                principal_entity_set = NullEntity('')
            key_pairs = generate_accessible_entity_key_values(containing_entity_set)
        else:
            principal_entity_set = NullEntity('')
            key_pairs = {}

        accessible_entity = AccessibleEntity(self._entity_set, key_pairs, principal_entity_set)
        return accessible_entity


class AccessibleEntity(object):
    def __init__(self, entity_set, key_pairs, principal_entity_set):
        self._entity_set = entity_set
        self._key_pairs = key_pairs
        self._principal_entity_set = principal_entity_set
        self._accessible_entity_path = ''

        self._entity_set_name = None
        self._init_entity_set_name()

    @property
    def entity_set(self):
        return self._entity_set

    @property
    def entity_set_name(self):
        return self._entity_set.name

    @property
    def principal_entity_name(self):
        return self._principal_entity_set.name

    @property
    def data(self):
        return self._key_pairs

    @property
    def path(self):
        self._build_entity_path()
        return self._accessible_entity_path

    def _build_entity_path(self):
        if self._key_pairs:
            self._accessible_entity_path = self._generate_addressable_path()
        else:
            self._accessible_entity_path = self._entity_set.name

    def _generate_addressable_path(self):
        if self._principal_entity_set.name:
            path = self._principal_entity_set.name + self._build_key_values() + '/' + self._entity_set_name
        else:
            path = self._entity_set.name + self._build_key_values()
        return path

    def _build_key_values(self):
        key_values = '(' + self._generate_key_pairs() + ')'
        return key_values

    def _generate_key_pairs(self):
        entity_path = ''
        for proprty_name, proprty_value in self._key_pairs.items():
            entity_path += proprty_name + '=' + proprty_value + ','
        entity_path = entity_path[:-1]
        return entity_path

    def _init_entity_set_name(self):
        # TODO: this is useless check
        if self._principal_entity_set.name:
            for navigation_prop in self._principal_entity_set.entity_type.nav_proprties:
                for end in self._entity_set.association_set_ends:
                    if navigation_prop.to_role.role == end.role:
                        self._entity_set_name = navigation_prop.name
                        break


def is_method(obj):
    return inspect.isfunction(obj) or inspect.ismethod(obj)


def get_methods_dict(class_object):
    filter_functions = inspect.getmembers(class_object, predicate=is_method)
    return {name: method for name, method in filter_functions if name.startswith('func_')}


def weighted_random(items):
    random_number = random.random()
    for value, weight in items:
        if random_number < weight:
            return value
        random_number -= weight
    return None


def build_filter_part(part):
    string_part = part['name'] + ' ' + part['operator'] + ' ' + part['operand']
    return string_part


def dict_by_id(object_list, identifier):
    for dictionary in object_list:
        if dictionary['id'] == identifier:
            return dictionary
    return None


def get_part_by_id(option_value, logical, selected_id):
    part = dict_by_id(option_value['parts'], logical[selected_id])
    if not part:
        part = dict_by_id(option_value['groups'], logical[selected_id])
    return part


def remove_by_reference(corresponding_list, corresponding_id):
    part_del = dict_by_id(corresponding_list, corresponding_id)
    if part_del:
        corresponding_list.remove(part_del)
    return part_del


def remove_logical_from_group(option_value, group_id, logical_id):
    group = dict_by_id(option_value['groups'], group_id)
    try:
        group['logicals'].remove(logical_id)
    except ValueError:
        pass


def get_principal_entities(data_model, entity_set):
    principal_entities = []
    for association_set in data_model.association_sets:
        ends_principal_getter = EndsPrincipal(association_set, entity_set)
        principal_entity = ends_principal_getter.get()
        if principal_entity:
            principal_entities.append(principal_entity)
        else:
            multiplicity_principal_getter = MultiplicityPrincipal(association_set, entity_set)
            principal_entity = multiplicity_principal_getter.get()
            if principal_entity:
                principal_entities.append(principal_entity)
    return principal_entities


class PrincipalGetter(metaclass=ABCMeta):
    def __init__(self, association_set, entity_set):
        self._association_set = association_set
        self._entity_set = entity_set

    def get(self):
        principal_entity = None
        if self.may_contain_principal_entity():
            index = 0
            for end in self._association_set.ends:
                if end.entity_set.name == self._entity_set.name:
                    principal_entity_index = index ^ 1
                    principal_entity = self.get_principal(principal_entity_index)
                    break
                index += 1
        return principal_entity

    def may_contain_principal_entity(self):
        return len(self._association_set.ends) == 2

    @abstractmethod
    def get_principal(self, index):
        pass


class EndsPrincipal(PrincipalGetter):
    def get_principal(self, index):
        principal_entity = None
        principal_end = self._association_set.ends[index]
        principal = getattr(self._association_set.association_type.referential_constraint, 'principal', None)
        if principal and principal.name == principal_end.role:
            principal_entity = principal_end.entity_set
        return principal_entity


class MultiplicityPrincipal(PrincipalGetter):
    def get_principal(self, index):
        principal_entity = None
        association_set_end = self._association_set.ends[index]
        if self._association_set.association_type.end_by_role(association_set_end.role).multiplicity == '1':
            principal_entity = association_set_end.entity_set
        return principal_entity


def generate_accessible_entity_key_values(containing_entity_set):
    key_pairs = {}
    for proprty in containing_entity_set.entity_type.key_proprties:
        key_pairs[proprty.name] = proprty.generate()
    return key_pairs


def get_draft_properties(entity_set_name, draft_objects):
    draft_properties = []
    if draft_objects:
        draft_properties = draft_objects.get(entity_set_name, [])
    return draft_properties
