from pydantic import Field

def db_field(
    *, 
    primary_key=False, 
    autoincrement=False, 
    unique=False,
    index=False, 
    foreign_key=None, 
    **kwargs
) -> Field:
    return Field(
        json_schema_extra={
            "db_primary_key": primary_key,
            "db_autoincrement": autoincrement,
            "db_unique": unique,
            "db_index": index,
            "db_foreign_key": foreign_key,
        },
        **kwargs
    )