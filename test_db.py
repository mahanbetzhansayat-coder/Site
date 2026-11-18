from db import get_connection

try:
    with get_connection() as conn:
        with conn.cursor() as cur:
            #Проверка текущего подключения
            cur.execute("""
                SELECT current_database(), current_schema(), current_user;
            """)
            print("Подключение:", cur.fetchone())

            #Поиск таблиц в information_schema
            cur.execute("""
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_name IN ('courses', 'lessons', 'categories')
                ORDER BY table_schema, table_name;
            """)
            print("\nНайденные таблицы:")
            for row in cur.fetchall():
                print(row)

            #Проверка существования таблиц 
            cur.execute("""
                SELECT to_regclass('elearning_v2.courses'),
                       to_regclass('public.courses');
            """)
            print("\nПроверка таблиц:", cur.fetchone())

except Exception as e:
    print("Ошибка:", e)