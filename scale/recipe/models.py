'''Defines the database models for recipes and recipe types'''
from __future__ import unicode_literals

import djorm_pgjson.fields
from django.db import models, transaction

from job.models import Job
from recipe.configuration.data.recipe_data import RecipeData
from recipe.configuration.definition.recipe_definition import RecipeDefinition
from storage.models import ScaleFile


# Important note: when acquiring select_for_update() locks on related models, be sure to acquire them in the following
# order: JobExecution, Recipe, Job, RecipeType, JobType, TriggerRule


class RecipeManager(models.Manager):
    '''Provides additional methods for handling recipes
    '''

    @transaction.atomic
    def create_recipe(self, recipe_type, event, data):
        '''Creates a new recipe for the given type and returns the saved recipe model. All jobs for the recipe will also
        be created. The given recipe type model must have already been saved in the database (it must have an ID). The
        given event model must have already been saved in the database (it must have an ID). All database changes occur
        in an atomic transaction.

        :param recipe_type: The type of the recipe to create
        :type recipe_type: :class:`recipe.models.RecipeType`
        :param event: The event that triggered the creation of this recipe
        :type event: :class:`trigger.models.TriggerEvent`
        :param data: JSON description defining the recipe data to run on
        :type data: dict
        :returns: The new recipe
        :rtype: :class:`recipe.models.Recipe`
        :raises InvalidData: If the recipe data is invalid
        '''

        if not recipe_type.is_active:
            raise Exception('Recipe type is no longer active')
        if event is None:
            raise Exception('Event that triggered recipe creation is required')

        recipe = Recipe()
        recipe.recipe_type = recipe_type
        recipe.recipe_type_rev = RecipeTypeRevision.objects.get_latest_revision(recipe_type.id)
        recipe.event = event
        recipe_definition = recipe.get_recipe_definition()

        # Validate recipe data
        recipe_data = RecipeData(data)
        recipe_definition.validate_data(recipe_data)
        recipe.data = data
        recipe.save()

        # Create recipe jobs and link them to the recipe
        jobs_by_name = self._create_recipe_jobs(recipe_definition, event)
        for job_name in jobs_by_name:
            recipe_job = RecipeJob()
            recipe_job.job = jobs_by_name[job_name]
            recipe_job.job_name = job_name
            recipe_job.recipe = recipe
            recipe_job.save()

        return recipe

    @transaction.atomic
    def _create_recipe_jobs(self, recipe_definition, event):
        '''Creates and saves the job models for the recipe with the given definition. The given event model must have
        already been saved in the database (it must have an ID). All database changes occur in an atomic transaction.

        :param recipe_definition: The recipe definition
        :type recipe_definition: :class:`recipe.configuration.definition.recipe_definition.RecipeDefinition`
        :param event: The event that triggered the creation of this recipe
        :type event: :class:`trigger.models.TriggerEvent`
        :returns: A dictionary with each recipe job name mapping to its new job model
        :rtype: dict of str -> :class:`job.models.Job`
        '''

        # Get a mapping of all job names to job types for the recipe
        job_types_by_name = recipe_definition.get_job_type_map()

        # Create an associated job for each recipe reference
        results = {}
        for job_name, job_type in job_types_by_name.iteritems():
            job = Job.objects.create_job(job_type, event)
            job.save()
            results[job_name] = job

        return results

    def get_locked_recipe_for_job(self, job_id):
        '''Returns the recipe model for the given job. The returned recipe model (None if job does not have a recipe)
        will have its related recipe_type field populated and have a lock obtained by select_for_update().

        :param job_id: The ID of the job
        :type job_id: int
        :returns: The job's recipe, possibly None, with populated recipe_type and model lock
        :rtype: :class:`recipe.models.Recipe`
        '''

        try:
            recipe_job = RecipeJob.objects.get(job_id=job_id)
            recipe_qry = Recipe.objects.select_related('recipe_type', 'recipe_type_rev').select_for_update()
            recipe = recipe_qry.get(pk=recipe_job.recipe_id)
        except RecipeJob.DoesNotExist:
            # Not in a recipe
            recipe = None

        return recipe

    def get_recipes(self, started=None, ended=None, type_ids=None, type_names=None, order=None):
        '''Returns a list of recipes within the given time range.

        :param started: Query recipes updated after this amount of time.
        :type started: :class:`datetime.datetime`
        :param ended: Query recipes updated before this amount of time.
        :type ended: :class:`datetime.datetime`
        :param type_ids: Query recipes of the type associated with the identifier.
        :type type_ids: list[int]
        :param type_names: Query recipes of the type associated with the name.
        :type type_names: list[str]
        :param order: A list of fields to control the sort order.
        :type order: list[str]
        :returns: The list of recipes that match the time range.
        :rtype: list[:class:`recipe.models.Recipe`]
        '''

        # Fetch a list of recipes
        recipes = Recipe.objects.all()
        recipes = recipes.select_related('recipe_type', 'recipe_type_rev', 'event')
        recipes = recipes.defer('recipe_type__definition', 'recipe_type_rev__recipe_type',
                                'recipe_type_rev__definition')

        # Apply time range filtering
        if started:
            recipes = recipes.filter(last_modified__gte=started)
        if ended:
            recipes = recipes.filter(last_modified__lte=ended)

        # Apply type filtering
        if type_ids:
            recipes = recipes.filter(recipe_type_id__in=type_ids)
        if type_names:
            recipes = recipes.filter(recipe_type__name__in=type_names)

        # Apply sorting
        if order:
            recipes = recipes.order_by(*order)
        else:
            recipes = recipes.order_by('last_modified')
        return recipes

    def get_details(self, recipe_id):
        '''Gets the details for a given recipe including its associated jobs and input files.

        :param recipe_id: The unique identifier of the recipe to fetch.
        :type recipe_id: :int
        :returns: A recipe with additional information.
        :rtype: :class:`recipe.models.Recipe`
        '''
        recipe = Recipe.objects.all()
        recipe = recipe.select_related('recipe_type', 'recipe_type_rev', 'event', 'event__rule')
        recipe = recipe.get(pk=recipe_id)

        # Update the recipe with source file models
        input_file_ids = recipe.get_recipe_data().get_input_file_ids()
        input_files = ScaleFile.objects.filter(id__in=input_file_ids)
        input_files = input_files.select_related('workspace').defer('workspace__json_config')
        input_files = input_files.order_by('id').distinct('id')
        recipe.input_files = [input_file for input_file in input_files]

        # Update the recipe with job models
        jobs = RecipeJob.objects.filter(recipe_id=recipe.id)
        jobs = jobs.select_related('job', 'job__job_type', 'job__event', 'job__error')
        recipe.jobs = jobs
        return recipe


class Recipe(models.Model):
    '''Represents a recipe to be run on the cluster. Any updates to a recipe model requires obtaining a lock on the
    model using select_for_update().

    :keyword recipe_type: The type of this recipe
    :type recipe_type: :class:`django.db.models.ForeignKey`
    :keyword recipe_type_rev: The revision of the recipe type when this recipe was created
    :type recipe_type_rev: :class:`django.db.models.ForeignKey`
    :keyword event: The event that triggered the creation of this recipe
    :type event: :class:`django.db.models.ForeignKey`

    :keyword data: JSON description defining the data for this recipe
    :type data: :class:`djorm_pgjson.fields.JSONField`

    :keyword created: When the recipe was created
    :type created: :class:`django.db.models.DateTimeField`
    :keyword completed: When every job in the recipe was completed successfully
    :type completed: :class:`django.db.models.DateTimeField`
    :keyword last_modified: When the recipe was last modified
    :type last_modified: :class:`django.db.models.DateTimeField`
    '''

    recipe_type = models.ForeignKey('recipe.RecipeType', on_delete=models.PROTECT)
    recipe_type_rev = models.ForeignKey('recipe.RecipeTypeRevision', on_delete=models.PROTECT)
    event = models.ForeignKey('trigger.TriggerEvent', on_delete=models.PROTECT)

    data = djorm_pgjson.fields.JSONField()

    created = models.DateTimeField(auto_now_add=True)
    completed = models.DateTimeField(blank=True, null=True)
    last_modified = models.DateTimeField(auto_now=True)

    objects = RecipeManager()

    def get_recipe_data(self):
        '''Returns the data for this recipe

        :returns: The data for this recipe
        :rtype: :class:`recipe.configuration.data.recipe_data.RecipeData`
        '''

        return RecipeData(self.data)

    def get_recipe_definition(self):
        '''Returns the definition for this recipe

        :returns: The definition for this recipe
        :rtype: :class:`recipe.configuration.definition.recipe_definition.RecipeDefinition`
        '''

        return RecipeDefinition(self.recipe_type_rev.definition)

    class Meta(object):
        '''meta information for the db'''
        db_table = 'recipe'


class RecipeJobManager(models.Manager):
    '''Provides additional methods for handling jobs linked to a recipe
    '''

    @transaction.atomic
    def get_recipe_jobs(self, recipe_id, jobs_related=False, jobs_lock=False):
        '''Returns the recipe_job models for the given recipe ID. Each recipe_job model with have its related job model
        populated.

        :param recipe_id: The recipe ID
        :type recipe_id: int
        :param jobs_related: Whether to include the related models (job_type, job_type_rev) on each related job model
        :type jobs_related: bool
        :param jobs_lock: Whether to obtain a select_for_update() lock on each related job model
        :type jobs_lock: bool
        :returns: The list of recipe jobs
        :rtype: list of :class:`recipe.models.RecipeJob`
        '''

        recipe_job_query = RecipeJob.objects.select_related('job').filter(recipe_id=recipe_id)
        recipe_jobs = list(recipe_job_query.iterator())

        if jobs_related or jobs_lock:
            job_ids = []
            jobs = {}
            for recipe_job in recipe_jobs:
                job_ids.append(recipe_job.job_id)
            # Query job models
            job_qry = Job.objects.all()
            if jobs_related:
                job_qry = job_qry.select_related('job_type', 'job_type_rev')
            if jobs_lock:
                job_qry = job_qry.select_for_update()
            for job in job_qry.filter(id__in=job_ids):
                jobs[job.id] = job
            # Re-populate the job fields with the updated job models
            for recipe_job in recipe_jobs:
                recipe_job.job = jobs[recipe_job.job_id]

        return recipe_jobs


class RecipeJob(models.Model):
    '''Links a job to its recipe

    :keyword job: A job in a recipe
    :type job: :class:`django.db.models.OneToOneField`
    :keyword job_name: The name of the job within the recipe
    :type job_name: :class:`django.db.models.CharField`
    :keyword recipe: The recipe that the job belongs to
    :type recipe: :class:`django.db.models.ForeignKey`
    '''

    job = models.OneToOneField('job.Job', primary_key=True, on_delete=models.PROTECT)
    job_name = models.CharField(max_length=100)
    recipe = models.ForeignKey('recipe.Recipe', on_delete=models.PROTECT)

    objects = RecipeJobManager()

    class Meta(object):
        '''meta information for the db'''
        db_table = 'recipe_job'


class RecipeTypeManager(models.Manager):
    '''Provides additional methods for handling recipe types
    '''

    @transaction.atomic
    def create_recipe_type(self, name, version, title, description, definition, trigger_rule):
        '''Creates a new recipe type and saves it in the database. All database changes occur in an atomic transaction.

        :param name: The system name of the recipe type
        :type name: str
        :param version: The version of the recipe type
        :type version: str
        :param title: The human-readable name of the recipe type
        :type title: str
        :param description: An optional description of the recipe type
        :type description: str
        :param definition: The definition for running a recipe of this type
        :type definition: :class:`recipe.configuration.definition.recipe_definition.RecipeDefinition`
        :param trigger_rule: The trigger rule that creates recipes of this type
        :type trigger_rule: :class:`trigger.models.TriggerRule`
        :returns: The new recipe type
        :rtype: :class:`recipe.models.RecipeType`

        :raises :class:`recipe.configuration.definition.exceptions.InvalidDefinition`:
            If any part of the recipe definition violates the specification
        '''

        # TODO: need to figure out how trigger rule validation works with this

        # Validate the recipe definition
        definition.validate_job_interfaces()

        # Create the new recipe type
        recipe_type = RecipeType()
        recipe_type.name = name
        recipe_type.version = version
        recipe_type.title = title
        recipe_type.description = description
        recipe_type.definition = definition.get_dict()
        recipe_type.trigger_rule = trigger_rule
        recipe_type.save()

        # Create first revision of the recipe type
        RecipeTypeRevision.objects.create_recipe_type_revision(recipe_type)

        return recipe_type

    def validate_recipe_type(self, name, version, description, definition):
        '''Validates a new recipe type prior to attempting a save

        :param name: The human-readable name of the recipe type
        :type name: str
        :param version: The version of the recipe type
        :type version: str
        :param description: An optional description of the recipe type
        :type description: str
        :param definition: The definition for running a recipe of this type
        :type definition: :class:`recipe.configuration.definition.recipe_definition.RecipeDefinition`
        :returns: A list of warnings discovered during validation.
        :rtype: list[:class:`job.configuration.data.job_data.ValidationWarning`]

        :raises :class:`recipe.configuration.definition.exceptions.InvalidDefinition`:
            If any part of the recipe definition violates the specification
        '''

        # TODO: need to figure out how trigger rule validation works with this
        return definition.validate_job_interfaces()

    def get_recipe_types(self, started=None, ended=None, order=None):
        '''Returns a list of recipe types within the given time range.

        :param started: Query recipe types updated after this amount of time.
        :type started: :class:`datetime.datetime`
        :param ended: Query recipe types updated before this amount of time.
        :type ended: :class:`datetime.datetime`
        :param order: A list of fields to control the sort order.
        :type order: list[str]
        :returns: The list of recipe types that match the time range.
        :rtype: list[:class:`recipe.models.RecipeType`]
        '''

        # Fetch a list of recipe types
        recipe_types = RecipeType.objects.all().defer('description')

        # Apply time range filtering
        if started:
            recipe_types = recipe_types.filter(last_modified__gte=started)
        if ended:
            recipe_types = recipe_types.filter(last_modified__lte=ended)

        # Apply sorting
        if order:
            recipe_types = recipe_types.order_by(*order)
        else:
            recipe_types = recipe_types.order_by('last_modified')
        return recipe_types

    def get_details(self, recipe_type_id):
        '''Gets additional details for the given recipe type model based on related model attributes.

        The additional fields include: job_types.

        :param recipe_type_id: The unique identifier of the recipe type.
        :type recipe_type_id: int
        :returns: The recipe type with extra related attributes.
        :rtype: :class:`recipe.models.RecipeType`
        '''

        # Attempt to fetch the requested recipe type
        recipe_type = RecipeType.objects.get(pk=recipe_type_id)

        # Add associated job type information
        recipe_type.job_types = recipe_type.get_recipe_definition().get_job_types()

        return recipe_type


class RecipeType(models.Model):
    '''Represents a type of recipe that can be run on the cluster. Any updates to a recipe type model requires obtaining
    a lock on the model using select_for_update().

    :keyword name: The stable name of the recipe type used by clients for queries
    :type name: :class:`django.db.models.CharField`
    :keyword version: The version of the recipe type
    :type version: :class:`django.db.models.CharField`
    :keyword title: The human-readable name of the recipe type
    :type title: :class:`django.db.models.CharField`
    :keyword description: An optional description of the recipe type
    :type description: :class:`django.db.models.CharField`

    :keyword is_active: Whether the recipe type is active (false once recipe type is archived)
    :type is_active: :class:`django.db.models.BooleanField`
    :keyword definition: JSON definition for running a recipe of this type
    :type definition: :class:`djorm_pgjson.fields.JSONField`
    :keyword trigger_rule: The rule to trigger new recipes of this type
    :type trigger_rule: :class:`django.db.models.ForeignKey`

    :keyword created: When the recipe type was created
    :type created: :class:`django.db.models.DateTimeField`
    :keyword archived: When the recipe type was archived (no longer active)
    :type archived: :class:`django.db.models.DateTimeField`
    :keyword last_modified: When the recipe type was last modified
    :type last_modified: :class:`django.db.models.DateTimeField`
    '''

    name = models.CharField(db_index=True, max_length=50)
    version = models.CharField(db_index=True, max_length=50)
    title = models.CharField(blank=True, max_length=50, null=True)
    description = models.CharField(blank=True, max_length=500)

    is_active = models.BooleanField(default=True)
    definition = djorm_pgjson.fields.JSONField()
    trigger_rule = models.ForeignKey('trigger.TriggerRule', blank=True, null=True, on_delete=models.PROTECT)

    created = models.DateTimeField(auto_now_add=True)
    archived = models.DateTimeField(blank=True, null=True)
    last_modified = models.DateTimeField(auto_now=True)

    objects = RecipeTypeManager()

    def get_recipe_definition(self):
        '''Returns the definition for running recipes of this type

        :returns: The recipe definition for this type
        :rtype: :class:`recipe.configuration.definition.recipe_definition.RecipeDefinition`
        '''

        return RecipeDefinition(self.definition)

    class Meta(object):
        '''meta information for the db'''
        db_table = 'recipe_type'
        unique_together = ('name', 'version')


class RecipeTypeRevisionManager(models.Manager):
    '''Provides additional methods for handling recipe type revisions
    '''

    def create_recipe_type_revision(self, recipe_type):
        '''Creates a new revision for the given recipe type. The caller must have obtained a lock using
        select_for_update() on the given recipe type model.

        :param recipe_type: The recipe type
        :type recipe_type: :class:`recipe.models.RecipeType`
        '''

        last_rev = self.get_latest_revision(recipe_type.id)
        new_rev_num = 1
        if last_rev:
            new_rev_num = last_rev.revision_num + 1

        new_rev = RecipeTypeRevision()
        new_rev.recipe_type = recipe_type
        new_rev.revision_num = new_rev_num
        new_rev.definition = recipe_type.definition
        new_rev.save()

    def get_latest_revision(self, recipe_type_id):
        '''Returns the latest revision for the given recipe type

        :param recipe_type_id: The ID of the recipe type
        :type recipe_type_id: int
        :returns: The latest revision for the recipe type
        :rtype: :class:`recipe.models.RecipeTypeRevision`
        '''

        return RecipeTypeRevision.objects.filter(recipe_type_id=recipe_type_id).order_by('-revision_num').first()


class RecipeTypeRevision(models.Model):
    '''Represents a revision of a recipe type. New revisions are created when the definition of a recipe type changes.
    Any inserts of a recipe type revision model requires obtaining a lock using select_for_update() on the corresponding
    recipe type model.

    :keyword recipe_type: The recipe type for this revision
    :type recipe_type: :class:`django.db.models.ForeignKey`
    :keyword revision_num: The number for this revision, starting at one
    :type revision_num: :class:`django.db.models.IntegerField`
    :keyword definition: The JSON definition for this revision of the recipe type
    :type definition: :class:`djorm_pgjson.fields.JSONField`
    :keyword created: When this revision was created
    :type created: :class:`django.db.models.DateTimeField`
    '''

    recipe_type = models.ForeignKey('recipe.RecipeType', on_delete=models.PROTECT)
    revision_num = models.IntegerField()
    definition = djorm_pgjson.fields.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    objects = RecipeTypeRevisionManager()

    def get_recipe_definition(self):
        '''Returns the recipe type definition for this revision

        :returns: The recipe type definition for this revision
        :rtype: :class:`recipe.configuration.definition.recipe_definition.RecipeDefinition`
        '''

        return RecipeDefinition(self.definition)

    class Meta(object):
        '''meta information for the db'''
        db_table = 'recipe_type_revision'
        unique_together = ('recipe_type', 'revision_num')
