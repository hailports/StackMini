# Goal targets

Level targets for the dealer program.

```text
level 1 = ROUND(goal * 0.90, 0)
level 2 = ROUND(goal, 0)
level 3 = ROUND(goal + 300000, 0)
          when 300000 <= goal <= 1000000
        = ROUND(goal * 1.20, 0)
          otherwise
```
