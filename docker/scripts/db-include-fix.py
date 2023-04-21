from pathlib import Path
content = Path('/workspace/ietf/settings_local.py').read_text()
newcontent = content.replace(
    """DATABASES = {
    'default': {
        'HOST': 'db',
        'PORT': 3306,
        'NAME': 'ietf_utf8',
        'ENGINE': 'django.db.backends.mysql',
        'USER': 'django',
        'PASSWORD': 'RkTkDPFnKpko',
        'OPTIONS': {
            'sql_mode': 'STRICT_TRANS_TABLES',
            'init_command': 'SET storage_engine=InnoDB; SET names "utf8"',
        },
    },
}""",
    "from ietf.settings_postgresqldb import DATABASES",
).replace(
    """DATABASES = {
    'default': {
        'HOST': 'pgdb',
        'PORT': 5432,
        'NAME': 'ietf',
        'ENGINE': 'django.db.backends.postgresql_psycopg2',
        'USER': 'django',
        'PASSWORD': 'RkTkDPFnKpko',
    },
}""",
    "from ietf.settings_postgresqldb import DATABASES",
)
with Path('/workspace/ietf/settings_local.py').open('w') as replacementfile:
    replacementfile.write(newcontent)
