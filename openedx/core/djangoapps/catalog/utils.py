"""Helper functions for working with the catalog service."""
import copy

from django.conf import settings
from django.core.cache import cache
from django.contrib.auth import get_user_model
from edx_rest_api_client.client import EdxRestApiClient
from opaque_keys.edx.keys import CourseKey

from openedx.core.djangoapps.catalog.models import CatalogIntegration
from openedx.core.lib.edx_api_utils import get_edx_api_data
from openedx.core.lib.token_utils import JwtBuilder
from xmodule.modulestore.django import modulestore


User = get_user_model()  # pylint: disable=invalid-name


def create_catalog_api_client(user, catalog_integration):
    """Returns an API client which can be used to make catalog API requests."""
    scopes = ['email', 'profile']
    expires_in = settings.OAUTH_ID_TOKEN_EXPIRATION
    jwt = JwtBuilder(user).build_token(scopes, expires_in)

    return EdxRestApiClient(catalog_integration.internal_api_url, jwt=jwt)


def get_programs(uuid=None, type=None):  # pylint: disable=redefined-builtin
    """Retrieve marketable programs from the catalog service.

    Keyword Arguments:
        uuid (string): UUID identifying a specific program.
        type (string): Filter programs by type (e.g., "MicroMasters" will only return MicroMasters programs).

    Returns:
        list of dict, representing programs.
        dict, if a specific program is requested.
    """
    catalog_integration = CatalogIntegration.current()
    if catalog_integration.enabled:
        try:
            user = User.objects.get(username=catalog_integration.service_username)
        except User.DoesNotExist:
            return []

        api = create_catalog_api_client(user, catalog_integration)

        cache_key = '{base}.programs{type}'.format(
            base=catalog_integration.CACHE_KEY,
            type='.' + type if type else ''
        )

        querystring = {
            'marketable': 1,
            'exclude_utm': 1,
        }
        if uuid:
            querystring['use_full_course_serializer'] = 1
        if type:
            querystring['type'] = type

        return get_edx_api_data(
            catalog_integration,
            user,
            'programs',
            resource_id=uuid,
            cache_key=cache_key if catalog_integration.is_cache_enabled else None,
            api=api,
            querystring=querystring,
        )
    else:
        return []


def get_program_type(name):
    """
    Retrieve the program type with the given name from the catalog service.

    Arguments:
        name (string): Name of the program type to retrieve.

    Returns:
        dict, representing the program type.
    """
    return next(program_type for program_type in get_program_types() if program_type['name'] == name)


def get_program_types():
    """Retrieve all program types from the catalog service.

    Returns:
        list of dict, representing program types.
    """
    catalog_integration = CatalogIntegration.current()
    if catalog_integration.enabled:
        try:
            user = User.objects.get(username=catalog_integration.service_username)
        except User.DoesNotExist:
            return []

        api = create_catalog_api_client(user, catalog_integration)
        cache_key = '{base}.program_types'.format(base=catalog_integration.CACHE_KEY)

        return get_edx_api_data(
            catalog_integration,
            user,
            'program_types',
            cache_key=cache_key if catalog_integration.is_cache_enabled else None,
            api=api
        )
    else:
        return []


def _get_program_instructors(program):
    """
    Returns a list of dicts representing the instructor profile data for each unique
    instructor associated with a course run in the program. This instructor profile data
    is stored on the course module. We cache the instructor info to avoid course module
    lookups on every call.

    Arguments:
        program (dict): representing the program.

    Returns:
        list of dict, representing the unique set of instructors for the program.
    """
    cache_key = 'program.instructors.{program_id}'.format(
        program_id=program.get('uuid')
    )

    program_instructors_dict = {}
    program_instructors_list = cache.get(cache_key, [])
    if program_instructors_list:
        return program_instructors_list

    module_store = modulestore()
    for course_run_key in _get_program_course_run_keys(program):
        course_descriptor = module_store.get_course(course_run_key)
        if course_descriptor:
            course_instructors = getattr(course_descriptor, 'instructor_info', {})
            # Deduplicate program instructors using instructor name
            program_instructors_dict.update(
                {instructor.get('name'): instructor for instructor in course_instructors.get('instructors', [])}
            )

    program_instructors_list = program_instructors_dict.values()
    cache.set(cache_key, program_instructors_list)

    return program_instructors_list


def _get_program_course_run_keys(program):
    """
    Returns a list of course keys for each course run in the given program.

    Arguments:
        program (dict): representing the program.

    Returns:
        list of CourseKey, for each course run key in the program.
    """
    keys = []
    for course in program.get('courses', []):  # pylint: disable=E1101
        for course_run in course.get('course_runs', []):
            keys.append(CourseKey.from_string(course_run.get('key')))
    return keys


def get_program_with_type_and_instructors(marketing_slug):
    """
    Return the program with full program type data and instructor info.
    The instructor info added here is stored on the course module and can be
    edited in Studio.

    Arguments:
        marketing_slug (string): The program marketing slug.

    Returns:
        dict, representing the program.
    """
    program_with_type_and_instructors = None
    programs = get_programs()

    # Find the program using the marketing slug.
    program = next((program for program in programs if program.get('marketing_slug') == marketing_slug), None)
    if program:
        # A call to get_programs() with no UUID makes a call
        # to the catalog service's programs list endpoint which
        # uses a serializer that excludes some program data
        # that we need here.
        # Get the program by UUID in order to get the fully serialized program.
        fully_serialized_program = next(iter(get_programs(uuid=program.get('uuid'))), None)

        if fully_serialized_program:
            # Deep copy the program dict here so we are not adding
            # the type and instructors to the cached object.
            program_with_type_and_instructors = copy.deepcopy(program)
            program_with_type_and_instructors['type'] = get_program_type(program['type'])
            program_with_type_and_instructors['instructors'] = _get_program_instructors(program)

    return program_with_type_and_instructors


def get_programs_with_type(include_types=None):
    """
    Return the list of programs. You can filter the types of programs returned using the optional
    include_types parameter. If no filter is provided, all programs of all types will be returned.

    Arguments:
        include_types (list): The program type filter.

    Return:
        list of dict, representing the active programs.
    """
    programs_with_type = []
    programs = get_programs()

    if programs:
        program_types = {program_type['name']: program_type for program_type in get_program_types()}
        for program in programs:
            if not include_types or program['type'] in include_types:
                # deepcopy the program dict here so we are not adding
                # the type to the cached object
                program_with_type = copy.deepcopy(program)
                program_with_type['type'] = program_types[program['type']]
                programs_with_type.append(program_with_type)

    return programs_with_type
